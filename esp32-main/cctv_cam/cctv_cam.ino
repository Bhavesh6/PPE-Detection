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
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include "esp_camera.h"
#include "esp_http_server.h"
#include "img_converters.h"      // frame2jpg(), for sensors with no encoder

// WIFI_SSID, WIFI_PASSWORD and CAM_ID live in secrets.h, next to this
// file - gitignored. The Arduino IDE picks up a same-folder header with
// no include path setup.
#include "secrets.h"

// Defaults when a board_*.h does not say otherwise. VGA/12 keeps the
// previous behaviour for any camera that has not been tuned.
#ifndef CAM_FRAMESIZE
#define CAM_FRAMESIZE FRAMESIZE_VGA
#endif
#ifndef CAM_QUALITY
#define CAM_QUALITY 12
#endif

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


/* Change resolution and JPEG quality without reflashing.

   Espressif's own CameraWebServer exposes this, and for good reason: the
   right frame size is a property of the link, not of the code. Measured
   here, VGA cost about 28KB a frame and the sensor managed 9.6 of them a
   second; QVGA is nearer a quarter of that and roughly doubles the rate.
   Which you want depends on whether you are watching over the LAN or
   through a tunnel on a phone hotspot, and that can change between one
   demo and the next.

   Deliberately not persisted. A camera that quietly keeps a setting
   somebody tried once is worse than one that returns to a known state
   when power-cycled - board_*.h stays the single source of truth.

     GET /control?var=framesize&val=6   (6=QVGA 320x240, 8=VGA 640x480)
     GET /control?var=quality&val=15    (10..63, lower = better = bigger)
*/
static esp_err_t control_handler(httpd_req_t *req) {
  char query[64];
  char var[16];
  char val[16];

  if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK ||
      httpd_query_key_value(query, "var", var, sizeof(var)) != ESP_OK ||
      httpd_query_key_value(query, "val", val, sizeof(val)) != ESP_OK) {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST,
                        "use /control?var=framesize|quality&val=<n>");
    return ESP_FAIL;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (!s) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "no sensor");
    return ESP_FAIL;
  }

  const int n = atoi(val);
  int rc = -1;
  if (!strcmp(var, "framesize")) {
    // Above SVGA the sensor drops frames badly and the buffers were sized
    // at boot, so refuse rather than wedge the stream.
    if (n < 0 || n > FRAMESIZE_SVGA) {
      httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "framesize out of range (0..FRAMESIZE_SVGA)");
      return ESP_FAIL;
    }
    rc = s->set_framesize(s, (framesize_t)n);
  } else if (!strcmp(var, "quality")) {
    if (n < 10 || n > 63) {
      httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "quality out of range (10..63)");
      return ESP_FAIL;
    }
    rc = s->set_quality(s, n);
  } else {
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "unknown var");
    return ESP_FAIL;
  }

  Serial.printf("[control] %s=%d -> %s\n", var, n, rc == 0 ? "ok" : "refused");
  httpd_resp_set_type(req, "text/plain");
  return httpd_resp_sendstr(req, rc == 0 ? "ok\n" : "sensor refused\n");
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
  httpd_uri_t ctrl_uri   = {"/control",  HTTP_GET, control_handler,  NULL};

  if (httpd_start(&server, &config) == ESP_OK) {
    httpd_register_uri_handler(server, &index_uri);
    httpd_register_uri_handler(server, &stream_uri);
    httpd_register_uri_handler(server, &snap_uri);
    httpd_register_uri_handler(server, &ctrl_uri);
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
  //
  // Both are overridable per board (board_*.h), because the right answer
  // differs by what the camera is for. Measured on this link: VGA at
  // quality 12 is ~17KB a frame, and the Pi's uplink - a phone hotspot
  // through a tunnel - carried about two of those a second. QVGA at 15 is
  // nearer 5KB, so the same uplink carries roughly three times as many.
  // That trade is worth taking here and nowhere else: PPE is decided from
  // the gate's own webcam, so nothing about a verdict depends on this
  // sensor's resolution. These frames are for watching, not for judging.
  config.frame_size   = psram ? CAM_FRAMESIZE : FRAMESIZE_QVGA;
  config.jpeg_quality = CAM_QUALITY;      // lower = better = bigger
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


/* ---- Optional: post frames to the backend without the Pi -------------

   Normally the Pi relays this camera, which is the better path: it holds
   a real device session and this board holds no credentials at all. But
   a camera whose only route out is through the Pi vanishes entirely
   while the Pi is off or rebooting, and "the yard is invisible because
   the gate restarted" is poor behaviour for a monitoring feed.

   With BACKEND_URL and UPLOAD_TOKEN set in secrets.h, the camera also
   posts for itself on a slow timer. When the Pi is up its faster relay
   simply overwrites these, so the two paths need no coordination — the
   console always shows whichever frame arrived last. When the Pi is
   down, this keeps the tile alive.

   The token is not a login. It can do exactly one thing, replace this
   camera's picture, which is about as much as should ever live in the
   flash of a board sitting on an open LAN.
*/
#if defined(BACKEND_URL) && defined(UPLOAD_TOKEN)

#ifndef UPLOAD_INTERVAL_MS
// Deliberately slower than the Pi's relay. This is a fallback, not a
// second feed: it should cost little while the Pi is doing the work.
#define UPLOAD_INTERVAL_MS 5000
#endif

static unsigned long last_upload = 0;

static void upload_frame() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) return;

  uint8_t *jpg = NULL; size_t jpg_len = 0; bool needs_free = false;
  if (!as_jpeg(fb, &jpg, &jpg_len, &needs_free)) {
    esp_camera_fb_return(fb);
    return;
  }

  String url = String(BACKEND_URL) + "/api/cctv/frame?id=" + CAM_ID;
  const bool secure = url.startsWith("https://");

  // Kept in the narrowest scope that works: a TLS session holds tens of
  // KB, and this board is already spending its RAM on frame buffers.
  WiFiClientSecure tls;
  WiFiClient plain;
  if (secure) {
    // No certificate store on this board, and no clock to check
    // validity against. This protects the token in transit but does not
    // prove which server received it - acceptable because that token
    // can only replace a picture, and worth knowing rather than
    // assuming otherwise.
    tls.setInsecure();
  }

  HTTPClient http;
  bool ok = secure ? http.begin(tls, url) : http.begin(plain, url);
  if (ok) {
    http.addHeader("Content-Type", "image/jpeg");
    http.addHeader("X-Device-Token", UPLOAD_TOKEN);
    int code = http.POST(jpg, jpg_len);

    // Only state changes are logged. A camera posting every 5s would
    // otherwise fill the console with identical success lines and bury
    // anything that mattered.
    static int last_code = 0;
    if (code != last_code) {
      if (code == 200)      Serial.println("[upload] backend accepting frames");
      else if (code > 0)    Serial.printf("[upload] backend returned %d\n", code);
      else                  Serial.printf("[upload] failed: %s\n",
                                          http.errorToString(code).c_str());
      last_code = code;
    }
    http.end();
  }

  if (needs_free) free(jpg);
  esp_camera_fb_return(fb);
}
#endif


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

  // Turn the radio down before associating. Observed on this board: a
  // clean POWERON boot, the camera initialising fine, then the ROM
  // banner appearing partway through "[wifi] connecting......" - over
  // and over. Nothing in this sketch restarts the chip and the connect
  // loop below has no timeout, so that reset comes from outside: the 5V
  // rail sagging under the transmit spike while the camera is also
  // drawing. An ESP32-CAM on a USB-serial adapter's supply is right at
  // the edge.
  //
  // 8.5dBm is roughly a quarter of full power. It costs range, which a
  // camera a few metres from the access point can afford, and buys one
  // that finishes booting. Fix the supply - a 1A source and a 470uF
  // capacitor across 5V/GND - and this can go back to default.
  WiFi.setTxPower(WIFI_POWER_8_5dBm);

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

#if defined(BACKEND_URL) && defined(UPLOAD_TOKEN)
  // Fallback path: keep the console's tile alive even with the Pi off.
  if (millis() - last_upload >= UPLOAD_INTERVAL_MS) {
    last_upload = millis();
    upload_frame();
  }
#endif

  delay(1000);
}
