/*
  SafetyFirst CCTV camera — an ESP32-CAM serving a live MJPEG stream on
  the LAN. A second pair of eyes on the site, separate from the gate: the
  checkpoint camera is busy deciding who gets in, and pointing it at the
  yard instead would mean choosing between the two.

  This board does not talk to the backend at all, and holds no
  credentials. It serves frames to whoever asks on the local network, and
  the Pi relays them onward (pi_app/cctv_relay.py -> backend/cctv.py) so
  the browser never connects to the camera directly. That split is
  deliberate:

    - The ESP32 does what it is good at (capture and serve) and nothing
      it is bad at. TLS plus a JWT refresh loop on a board with this much
      RAM buys latency and crashes, not security.
    - Access control stays in one place. The console already knows who is
      an admin; the camera would have to be taught, badly.
    - It keeps the camera off the public internet. Only the Pi beside it
      needs a route, and a camera down a shaft has no other route anyway.

  The corollary is that this camera is unauthenticated on its own
  network. Anyone on that WiFi can watch it by IP. Treat the network as
  the security boundary, and do not put this on a guest or public SSID.

  TWO SENSORS, ONE SKETCH
  -----------------------
  These boards ship with different camera modules and the difference is
  not cosmetic:

    OV2640            has a hardware JPEG encoder. Frames come out ready
                      to send, so VGA at a useful rate is cheap.
    GC2145            (sold as RHYX-M21-45 / M12-45) has no JPEG encoder
                      at all - only RGB565/YUV/RAW. Every frame must be
                      compressed in software on the CPU, which is slow,
                      so it runs at QVGA.

  Rather than keeping two sketches or a #define nobody remembers to
  change, this initialises optimistically in JPEG mode and falls back to
  RGB565 with software encoding if the sensor refuses. The Serial log
  says which sensor was found and which path is in use, because "the
  picture is slow" and "the picture is missing" have very different
  causes on these two parts.

  Endpoints (identical whichever sensor is fitted):
    /           a one-page status/preview, handy for proving it works
    /stream     multipart MJPEG - the live feed
    /snapshot   a single JPEG - what the Pi's relay polls

  Setup:
    1. Arduino IDE -> Boards Manager -> install "esp32" (Espressif).
       No extra libraries: esp_camera and esp_http_server ship with it.
    2. Copy secrets.h.example to secrets.h and fill in WiFi and CAM_ID.
       Gitignored, same pattern as ppe_sensors and alert_sim.
    3. Tools -> Board -> "AI Thinker ESP32-CAM".
       Tools -> Partition Scheme -> "Huge APP (3MB No OTA)". The default
       does not fit and the upload fails late, after compiling, with a
       size error that reads like a code problem.
    4. Flashing needs a USB-TTL adapter - this board has no USB port:
         adapter 5V->5V, GND->GND, TX->U0R, RX->U0T
         jumper IO0 to GND, press RESET to enter the bootloader
       Remove the jumper and press RESET again to run. "Failed to
       connect" is almost always IO0 not grounded when the upload starts.
    5. Serial Monitor at 115200 to see the sensor and the address.

  Power is the thing that actually bites. This board browns out on a weak
  5V supply the instant the radio transmits - it shows up as a boot loop,
  or a stream that dies a few seconds in, and looks like a firmware bug.
  Use a supply good for 500mA+ and short wires. Many USB-TTL adapters
  cannot deliver that from their 5V pin.
*/

#include <WiFi.h>
#include <ESPmDNS.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include "img_converters.h"      // frame2jpg(), for sensors with no encoder

// WIFI_SSID, WIFI_PASSWORD and CAM_ID live in secrets.h, next to this
// file - gitignored. The Arduino IDE picks up a same-folder header with
// no include path setup.
#include "secrets.h"

// Each board needs its own id. It becomes the mDNS name AND the key the
// console files frames under, so two cameras sharing one id would
// overwrite each other and the feed would flicker between two places.
#ifndef CAM_ID
#define CAM_ID "cam"
#endif

// The OV2640 on these boards is commonly mounted inverted, but not on
// every module - so it is a setting rather than a hardcoded flip. Change
// it here, not in CSS: /snapshot has no CSS, and every viewer would
// otherwise have to know.
#ifndef CAM_VFLIP
#define CAM_VFLIP 1
#endif
#ifndef CAM_HMIRROR
#define CAM_HMIRROR 1
#endif

// Advertised as safetyfirst-<CAM_ID>.local, so the address survives a
// DHCP lease change. Every moving identifier in this project has cost an
// outage once already - serial port numbers, tunnel hostnames - and an
// IP that shifts on reboot is the same trap.
#define MDNS_NAME "safetyfirst-" CAM_ID

// ---- AI-Thinker ESP32-CAM pin map ------------------------------------
// These differ per board. On the wrong map the sketch still compiles,
// connects to WiFi, and serves a blank or garbled frame - so if the
// picture is wrong, suspect this before suspecting the lens.
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// The white flash LED. Bright, hot, and a battery killer - left off. It
// shares a pin with the microSD data line, which is why SD is unused.
#define FLASH_LED_GPIO     4

// Software JPEG quality when the sensor cannot encode for us. Higher
// than the hardware path's setting because every point costs CPU time on
// a chip that is already the bottleneck.
#define SOFT_JPEG_QUALITY 80

static httpd_handle_t server = NULL;
static bool hw_jpeg = true;              // sensor encodes JPEG itself
static const char *sensor_label = "unknown";

static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=frame";
static const char *STREAM_BOUNDARY = "\r\n--frame\r\n";
static const char *STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";


static const char *sensor_name(int pid) {
  switch (pid) {
    case 0x26:   return "OV2640";
    case 0x2145: return "GC2145 (RHYX-M21-45)";
    case 0x3660: return "OV3660";
    case 0x5640: return "OV5640";
    case 0x0308: return "GC0308";
    case 0x232a: return "GC032A";
    default:     return "unrecognised";
  }
}


/* Hand back a JPEG for this frame, encoding in software when the sensor
   could not. Caller must call release() with what comes back. */
static bool as_jpeg(camera_fb_t *fb, uint8_t **out, size_t *len, bool *needs_free) {
  if (fb->format == PIXFORMAT_JPEG) {
    *out = fb->buf;
    *len = fb->len;
    *needs_free = false;
    return true;
  }
  *needs_free = true;
  return frame2jpg(fb, SOFT_JPEG_QUALITY, out, len);
}


static esp_err_t index_handler(httpd_req_t *req) {
  char page[640];
  snprintf(page, sizeof(page),
    "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>SafetyFirst %s</title>"
    "<style>body{margin:0;background:#111;color:#eee;font:14px system-ui;text-align:center}"
    "img{max-width:100%%;height:auto;display:block;margin:0 auto}"
    "p{padding:8px;margin:0}small{color:#888}</style>"
    "<p>SafetyFirst CCTV &mdash; <b>%s</b><br><small>%s, %s JPEG</small></p>"
    "<img src='/stream' alt='live view'>",
    CAM_ID, CAM_ID, sensor_label, hw_jpeg ? "hardware" : "software");

  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, page, HTTPD_RESP_USE_STRLEN);
}


static esp_err_t snapshot_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  uint8_t *jpg = NULL;
  size_t jpg_len = 0;
  bool needs_free = false;
  if (!as_jpeg(fb, &jpg, &jpg_len, &needs_free)) {
    esp_camera_fb_return(fb);
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=snapshot.jpg");
  // No caching: a still frame served from cache is worse than no frame,
  // because it looks current.
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  esp_err_t res = httpd_resp_send(req, (const char *)jpg, jpg_len);

  if (needs_free) free(jpg);
  esp_camera_fb_return(fb);
  return res;
}


static esp_err_t stream_handler(httpd_req_t *req) {
  char part[64];

  esp_err_t res = httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");

  while (true) {
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      // One dropped grab is not worth ending the stream over; the viewer
      // would see a dead image and have to reload. Skip the frame.
      Serial.println("[cam] frame grab failed");
      continue;
    }

    uint8_t *jpg = NULL;
    size_t jpg_len = 0;
    bool needs_free = false;
    if (!as_jpeg(fb, &jpg, &jpg_len, &needs_free)) {
      esp_camera_fb_return(fb);
      Serial.println("[cam] jpeg conversion failed");
      continue;
    }

    size_t n = snprintf(part, sizeof(part), STREAM_PART, jpg_len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part, n);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)jpg, jpg_len);

    if (needs_free) free(jpg);
    esp_camera_fb_return(fb);

    // The viewer closed the tab, or the relay hung up. Not an error -
    // it is how every stream ends.
    if (res != ESP_OK) break;
  }

  return ESP_OK;
}


static void start_server() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  // The stream handler never returns while a viewer is connected, so it
  // holds its socket for the whole session. Without headroom, one viewer
  // blocks /snapshot - which is exactly what the Pi's relay polls.
  config.max_open_sockets = 4;

  httpd_uri_t index_uri  = {"/",         HTTP_GET, index_handler,    NULL};
  httpd_uri_t stream_uri = {"/stream",   HTTP_GET, stream_handler,   NULL};
  httpd_uri_t snap_uri   = {"/snapshot", HTTP_GET, snapshot_handler, NULL};

  if (httpd_start(&server, &config) == ESP_OK) {
    httpd_register_uri_handler(server, &index_uri);
    httpd_register_uri_handler(server, &stream_uri);
    httpd_register_uri_handler(server, &snap_uri);
    Serial.println("[http] server up on :80");
  } else {
    Serial.println("[http] server FAILED to start");
  }
}


static void fill_pins(camera_config_t *c) {
  c->ledc_channel = LEDC_CHANNEL_0;
  c->ledc_timer   = LEDC_TIMER_0;
  c->pin_d0       = Y2_GPIO_NUM;
  c->pin_d1       = Y3_GPIO_NUM;
  c->pin_d2       = Y4_GPIO_NUM;
  c->pin_d3       = Y5_GPIO_NUM;
  c->pin_d4       = Y6_GPIO_NUM;
  c->pin_d5       = Y7_GPIO_NUM;
  c->pin_d6       = Y8_GPIO_NUM;
  c->pin_d7       = Y9_GPIO_NUM;
  c->pin_xclk     = XCLK_GPIO_NUM;
  c->pin_pclk     = PCLK_GPIO_NUM;
  c->pin_vsync    = VSYNC_GPIO_NUM;
  c->pin_href     = HREF_GPIO_NUM;
  c->pin_sccb_sda = SIOD_GPIO_NUM;
  c->pin_sccb_scl = SIOC_GPIO_NUM;
  c->pin_pwdn     = PWDN_GPIO_NUM;
  c->pin_reset    = RESET_GPIO_NUM;
  c->xclk_freq_hz = 20000000;
  c->grab_mode    = CAMERA_GRAB_LATEST;   // a live view wants the newest
                                          // frame, not the oldest queued
}


static bool start_camera() {
  const bool psram = psramFound();

  // Attempt 1: hardware JPEG. Works on OV2640 and the other OV parts.
  camera_config_t config = {};
  fill_pins(&config);
  config.pixel_format = PIXFORMAT_JPEG;
  config.fb_location  = psram ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  // Frame size is bounded by memory, not by taste. With PSRAM there is
  // room to double-buffer at VGA; without it, asking for VGA fails to
  // allocate and the camera never initialises at all.
  config.frame_size   = psram ? FRAMESIZE_VGA : FRAMESIZE_QVGA;
  config.jpeg_quality = 12;               // lower = better = bigger
  config.fb_count     = psram ? 2 : 1;

  esp_err_t err = esp_camera_init(&config);

  if (err != ESP_OK) {
    // Attempt 2: no hardware encoder (GC2145 and friends). RGB565 out of
    // the sensor, compressed on the CPU per request. QVGA and a single
    // buffer, because software JPEG is expensive and a raw VGA frame is
    // 600KB before it is even compressed.
    Serial.printf("[cam] JPEG mode refused (0x%x) - retrying as RGB565\n", err);
    esp_camera_deinit();

    fill_pins(&config);
    config.pixel_format = PIXFORMAT_RGB565;
    config.frame_size   = FRAMESIZE_QVGA;
    config.fb_location  = psram ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
    config.fb_count     = 1;

    err = esp_camera_init(&config);
    if (err != ESP_OK) {
      Serial.printf("[cam] init failed in both modes: 0x%x\n", err);
      return false;
    }
    hw_jpeg = false;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    sensor_label = sensor_name(s->id.PID);
    Serial.printf("[cam] sensor: %s (PID 0x%04x)\n", sensor_label, s->id.PID);
    s->set_vflip(s, CAM_VFLIP);
    s->set_hmirror(s, CAM_HMIRROR);
  }

  Serial.printf("[cam] %s JPEG, %s\n",
                hw_jpeg ? "hardware" : "software (slower)",
                psram ? "PSRAM found" : "no PSRAM - reduced resolution");
  return true;
}


void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nSafetyFirst CCTV camera — id \"%s\"\n", CAM_ID);

  // Off, and explicitly so. It defaults low, but a floating pin on a
  // board that has browned out has been known to light it.
  pinMode(FLASH_LED_GPIO, OUTPUT);
  digitalWrite(FLASH_LED_GPIO, LOW);

  if (!start_camera()) {
    Serial.println("[cam] giving up - check the ribbon cable seating and 5V supply");
    return;
  }
  Serial.println("[cam] ready");

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);          // modem sleep stutters an MJPEG stream badly
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("[wifi] connecting");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("[wifi] ip: ");
  Serial.println(WiFi.localIP());

  if (MDNS.begin(MDNS_NAME)) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("[mdns] http://%s.local/\n", MDNS_NAME);
  } else {
    // Not fatal - the IP above still works. Worth saying out loud
    // because the Pi's relay may be configured to use the name.
    Serial.println("[mdns] failed; use the IP address instead");
  }

  start_server();
  Serial.printf("[ready] stream: http://%s/stream\n", WiFi.localIP().toString().c_str());
  Serial.printf("[ready] relay this as: %s=http://%s.local\n", CAM_ID, MDNS_NAME);
}


void loop() {
  // Reconnect if the AP drops. The HTTP server survives a reconnect, so
  // there is nothing to restart - the next request simply succeeds. A
  // camera that silently stays offline until someone power-cycles it is
  // the failure mode worth avoiding here.
  static bool was_down = false;

  if (WiFi.status() != WL_CONNECTED) {
    if (!was_down) {
      Serial.println("[wifi] lost - reconnecting");
      was_down = true;
    }
    WiFi.reconnect();
    delay(2000);
    return;
  }

  if (was_down) {
    was_down = false;
    Serial.print("[wifi] back: ");
    Serial.println(WiFi.localIP());
  }
  delay(1000);
}
