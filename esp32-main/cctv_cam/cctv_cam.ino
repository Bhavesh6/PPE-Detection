/*
  SafetyFirst CCTV camera — an AI-Thinker ESP32-CAM serving a live MJPEG
  stream on the LAN. A second pair of eyes on the site, separate from the
  gate: the checkpoint camera is busy deciding who gets in, and pointing it
  at the yard instead would mean choosing between the two.

  This board does not talk to the backend at all, and holds no credentials.
  It serves frames to whoever asks on the local network, and the backend
  proxies that stream to the console (backend/cctv.py) so the browser never
  connects to the camera directly. That split is deliberate:

    - The ESP32 does what it is good at (capture and serve JPEG) and
      nothing it is bad at. TLS plus a JWT refresh loop on a board with
      this much RAM buys latency and crashes, not security.
    - Access control stays in one place. The console already knows who is
      an admin; the camera would have to be taught, badly.
    - It keeps the camera off the public internet. Only the backend needs
      to reach it, and the backend is already the thing that's exposed.

  The corollary is that this camera is unauthenticated on its own network.
  Anyone on that WiFi can watch it by IP. Treat the network as the security
  boundary, and do not put this on a guest or public SSID.

  Endpoints:
    /           a one-page status/preview, handy for proving it works
    /stream     multipart MJPEG - the live feed
    /snapshot   a single JPEG - cheaper when you only need one frame

  Setup:
    1. Arduino IDE -> Boards Manager -> install "esp32" (Espressif Systems).
       No extra libraries: esp_camera and esp_http_server ship with it.
    2. Copy secrets.h.example (same folder) to secrets.h and fill in your
       WiFi. Gitignored, same pattern as ppe_sensors and alert_sim.
    3. Tools -> Board -> "AI Thinker ESP32-CAM".
       Tools -> Partition Scheme -> "Huge APP (3MB No OTA)". The default
       scheme does not fit this sketch and the upload fails late, after
       compiling, with a size error that reads like a code problem.
    4. Flashing needs a USB-TTL adapter - this board has no USB port:
         adapter 5V->5V, GND->GND, TX->U0R, RX->U0T
         jumper IO0 to GND to enter bootloader, then press RESET
       Remove the IO0 jumper and press RESET again to run normally. A
       "Failed to connect" error is almost always IO0 not grounded at the
       moment the upload starts.
    5. Serial Monitor at 115200 to see the address it came up on.

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
#include "esp_timer.h"

// WIFI_SSID and WIFI_PASSWORD live in secrets.h, next to this file -
// gitignored. Copy secrets.h.example to secrets.h and fill it in; the
// Arduino IDE picks up a same-folder header with no include path setup.
#include "secrets.h"

// Advertised as MDNS_NAME.local, so the console can be pointed at a name
// that survives a DHCP lease change. Every moving identifier in this
// project has cost us an outage once already - serial port numbers, tunnel
// hostnames - and an IP that shifts on reboot is the same trap.
#define MDNS_NAME "safetyfirst-cam"

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

// The white flash LED. Bright, hot, and a battery killer - left off. It is
// on the same pin as the microSD slot's data line, which is why the SD
// card is not used here.
#define FLASH_LED_GPIO     4

static httpd_handle_t server = NULL;

static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=frame";
static const char *STREAM_BOUNDARY = "\r\n--frame\r\n";
static const char *STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static const char INDEX_HTML[] PROGMEM =
    "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
    "<title>SafetyFirst camera</title>"
    "<style>body{margin:0;background:#111;color:#eee;font:14px system-ui;text-align:center}"
    "img{max-width:100%;height:auto;display:block;margin:0 auto}"
    "p{padding:8px;margin:0}</style>"
    "<p>SafetyFirst CCTV &mdash; live</p>"
    "<img src='/stream' alt='live view'>";


static esp_err_t index_handler(httpd_req_t *req) {
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, (const char *)INDEX_HTML, HTTPD_RESP_USE_STRLEN);
}


static esp_err_t snapshot_handler(httpd_req_t *req) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }

  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=snapshot.jpg");
  // No caching: a still frame served from cache is worse than no frame,
  // because it looks current.
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);

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

    size_t len = snprintf(part, sizeof(part), STREAM_PART, fb->len);
    res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, part, len);
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);

    esp_camera_fb_return(fb);

    // The viewer closed the tab, or the proxy hung up. Not an error -
    // it is how every stream ends.
    if (res != ESP_OK) break;
  }

  return ESP_OK;
}


static void start_server() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = 80;
  // The stream handler never returns while a viewer is connected, so it
  // occupies its socket for the whole session. Without headroom here the
  // one viewer blocks /snapshot and / entirely.
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


static bool start_camera() {
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;
  config.grab_mode    = CAMERA_GRAB_LATEST;   // a live view wants the newest
                                              // frame, not the oldest queued one
  config.fb_location  = CAMERA_FB_IN_PSRAM;

  // Frame size is bounded by memory, not by taste. With PSRAM there is
  // room to double-buffer at VGA; without it, asking for VGA fails to
  // allocate and the camera never initialises at all - so drop to QVGA
  // and a single buffer rather than not starting.
  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;      // 640x480
    config.jpeg_quality = 12;                 // lower number = better = bigger
    config.fb_count     = 2;
  } else {
    config.frame_size   = FRAMESIZE_QVGA;     // 320x240
    config.jpeg_quality = 15;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    Serial.println("[cam] no PSRAM found - falling back to QVGA");
  }

  esp_err_t err = esp_camera_init(&config);
  if (err != ESP_OK) {
    Serial.printf("[cam] init failed: 0x%x\n", err);
    return false;
  }

  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    // The OV2640 on these boards is commonly mounted upside down. Flip it
    // here rather than rotating in CSS: every viewer would otherwise have
    // to know, and the snapshot endpoint has no CSS at all.
    s->set_vflip(s, 1);
    s->set_hmirror(s, 1);
  }
  return true;
}


void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\nSafetyFirst CCTV camera");

  // Off, and explicitly so. It defaults low, but a floating pin on a board
  // that has browned out has been known to light it.
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
    // Not fatal - the IP above still works. Worth saying out loud because
    // the console may be configured to use the name.
    Serial.println("[mdns] failed; use the IP address instead");
  }

  start_server();
  Serial.printf("[ready] stream: http://%s/stream\n", WiFi.localIP().toString().c_str());
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
