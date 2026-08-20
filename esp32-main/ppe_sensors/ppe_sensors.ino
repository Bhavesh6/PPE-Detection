/*
  SafetyFirst ESP32 sensor node — reads real hardware: an MQ-9 gas sensor
  on an analog pin, a DHT11 temperature/humidity sensor on a digital pin.
  Same sign-in-then-POST pattern as esp32-sim/alert_sim.ino (see that
  file's header for the fuller explanation) — that sketch stays put as a
  hardware-free fallback. If this board's wiring breaks at the venue and
  you still need to demo the alert pipeline, flash alert_sim.ino instead;
  nothing on the backend cares which one is talking to it.

  Wiring:
    MQ-9   VCC->5V  GND->GND  AOUT->10k->[node]->10k->GND, [node]->GPIO0
           (the divider halves AOUT's ~5V swing to a safe ~2.5V max, inside
            the ESP32-C3's ADC input range). DOUT unused, left unconnected.

    DHT11  VCC->5V  GND->GND  DATA->GPIO4
           (the bare 4-pin sensor also needs a 10k pull-up from DATA to
            5V; the 3-pin breakout module already has one built in, so
            skip it if that's what you have)

  Gas is reported in millivolts, not ppm. A raw MQ-9 has no ppm without
  Rs/R0 calibration against a known reference gas concentration, which
  nobody doing this at a hackathon actually has on hand — reporting the
  honest unit is the defensible choice over fabricating a number that
  looks precise but isn't. Once you've watched a few minutes of clean-air
  baseline in Serial Monitor, set a real threshold on the Alerts page
  above that baseline, with its unit changed to "mV" (it's currently
  configured for "ppm" from testing with the simulator — that number and
  unit no longer mean anything against a real reading).

  Serial Monitor commands (115200 baud, newline line ending):
    gas                     critical gas alert (manual trigger, /api/gate/alerts)
    smoke                   critical smoke alert (no smoke sensor wired; manual only)
    warn                    a non-critical warning (doesn't hold the gate)
    reading <kind> <value>  a raw value, e.g. "reading gas 450" — same path
                            the automatic sensor readings below use
    auto                    toggle automatic reporting: real MQ-9 gas (mV)
                            and real DHT11 temperature/humidity, read and
                            posted every few seconds
    status                  reprint WiFi/sign-in state

  Setup:
    1. Arduino IDE -> Boards Manager -> install "esp32" (Espressif Systems).
    2. Library Manager -> install "ArduinoJson" (by Benoit Blanchon, v6.x).
    3. Library Manager -> install "DHT sensor library" (by Adafruit). It
       will prompt to also install "Adafruit Unified Sensor" as a
       dependency — accept that too, both are required.
    4. Copy secrets.h.example (same folder) to secrets.h and fill in real
       values. Gitignored, so a real WiFi password never ends up in the
       tracked .ino — same pattern as esp32-sim/alert_sim.
    5. Tools -> Board -> ESP32C3 Dev Module. On an ESP32-C3 SuperMini also
       set USB CDC On Boot -> Enabled, or Serial Monitor stays blank.
    6. Upload, then Tools -> Serial Monitor, 115200 baud, line ending
       "Newline".

  API_BASE must be your PC's LAN IP (e.g. http://192.168.1.9:5000), not
  localhost or 127.0.0.1 — the ESP32 is a separate device on the network.
  Both need to be on the same WiFi, and the backend must be running.
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>

// WIFI_SSID, WIFI_PASSWORD, API_BASE, DEVICE_EMAIL, DEVICE_PASSWORD live in
// secrets.h, next to this file — gitignored. Copy secrets.h.example to
// secrets.h and fill in real values; the Arduino IDE picks up a
// same-folder header automatically, no include path setup needed.
#include "secrets.h"

#define MQ9_PIN 0    // GPIO0 (ADC1_CH0) — divided MQ-9 AOUT
#define DHT_PIN 4    // GPIO4 — DHT11 data line
#define DHT_TYPE DHT11

DHT dht(DHT_PIN, DHT_TYPE);

String authToken;

// State for the "auto" toggle — declared here, ahead of every function,
// because C++ doesn't hoist globals the way loop()/setup() might suggest:
// a function defined earlier in the file can't see a variable declared
// later, only a forward-declared or already-seen one.
bool autoMode = false;
unsigned long lastAutoSend = 0;
// DHT11's datasheet minimum is roughly 1s between reads; 4s is
// comfortably above that and matches the simulator's cadence, so a
// threshold configured while testing with alert_sim behaves the same way
// once this sketch is flashed instead.
const unsigned long AUTO_INTERVAL_MS = 4000;

// Reused across every request rather than constructed per-call — TLS
// session resumption needs the same client object, and setInsecure() only
// needs setting once.
WiFiClientSecure secureClient;

// Picks plain TCP or TLS based on the URL's own scheme, so the same
// sketch works whether API_BASE is a local "http://" LAN address (while
// developing) or a hosted "https://" one (once deployed) — nothing to
// remember to flip when that changes, since it reads it from the URL that
// was already going to be built anyway.
bool beginRequest(HTTPClient &http, const String &url) {
  if (url.startsWith("https://")) {
    return http.begin(secureClient, url);
  }
  return http.begin(url);
}

bool signIn() {
  HTTPClient http;
  bool useCredentials = strlen(DEVICE_EMAIL) > 0 && strlen(DEVICE_PASSWORD) > 0;
  String url = String(API_BASE) + (useCredentials ? "/api/auth/login" : "/api/auth/guest");
  beginRequest(http, url);
  http.addHeader("Content-Type", "application/json");

  int code;
  if (useCredentials) {
    StaticJsonDocument<192> body;
    body["email"] = DEVICE_EMAIL;
    body["password"] = DEVICE_PASSWORD;
    String payload;
    serializeJson(body, payload);
    code = http.POST(payload);
  } else {
    code = http.POST("{}");
  }

  // /api/auth/login returns 200, /api/auth/guest returns 201 (created) —
  // both are success.
  if (code != 200 && code != 201) {
    Serial.printf("Sign-in failed: HTTP %d\n", code);
    http.end();
    return false;
  }

  StaticJsonDocument<1024> resp;
  DeserializationError err = deserializeJson(resp, http.getString());
  http.end();
  if (err || !resp["success"]) {
    Serial.println("Sign-in rejected by the backend.");
    return false;
  }

  authToken = resp["token"].as<String>();
  Serial.print("Signed in as: ");
  Serial.println(resp["user"]["name"].as<const char *>());
  return true;
}

// Shared by both report functions below — same sign-in-if-needed,
// POST-with-one-retry-on-401 shape either way, only the path and body
// differ.
int postJson(const char *path, const String &payload) {
  if (authToken.isEmpty() && !signIn()) {
    Serial.println("Not signed in — cannot report.");
    return -1;
  }

  HTTPClient http;
  beginRequest(http, String(API_BASE) + path);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + authToken);
  int code = http.POST(payload);

  // A 401 means the token expired or the backend restarted (in-memory JWT
  // secret changed) — one retry after a fresh sign-in covers both without
  // needing a reboot.
  if (code == 401 && signIn()) {
    http.end();
    beginRequest(http, String(API_BASE) + path);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", "Bearer " + authToken);
    code = http.POST(payload);
  }

  Serial.printf("POST %s -> HTTP %d\n", path, code);
  if (code > 0) Serial.println(http.getString());
  http.end();
  return code;
}

/* ---- Optional: report via the gate master instead of WiFi -----------

   Define REPORT_VIA_ESPNOW in secrets.h to send readings and alerts to
   the gate master over ESP-NOW, which forwards them to the Pi on its USB
   line. This is the arrangement for a node with no network of its own -
   down a shaft, inside a tunnel, or simply out of range of the site AP.
   The Pi is the only box with a backhaul, and this puts the sensor
   behind it like everything else.

   THIS IS EITHER/OR, NOT A FALLBACK, and the reason is physical. The
   master deliberately never joins an AP so it stays on a fixed channel.
   A node that joins WiFi is moved to the router's channel by the radio,
   and ESP-NOW between two channels silently does nothing - no error, no
   packet, just a sensor that appears to work and reports to nobody. So a
   node either joins WiFi and posts over HTTP, or stays off WiFi and
   talks to the master. Trying to do both is how you get the quiet
   failure.

   Set MASTER_MAC to the address the master prints at boot:
       # ESP-NOW ready, mac 24:6F:28:AA:BB:CC, channel 1
   and ESPNOW_CHANNEL to the channel on that same line.
*/
#ifdef REPORT_VIA_ESPNOW
#include <esp_now.h>
#include <esp_wifi.h>

#ifndef ESPNOW_CHANNEL
#define ESPNOW_CHANNEL 1
#endif

/* Must stay byte-identical to the struct in gate_master.ino: the master
   drops any packet whose length does not match exactly, so a field added
   on one side and not the other means silence rather than an error. */
typedef struct {
  char  kind[16];      // "gas", "smoke", ...
  float value;         // raw reading
  char  unit[8];       // "ppm", "mV", "" if unitless
  char  severity[10];  // "" to let the Pi classify from the site thresholds
} SensorPacket;

static uint8_t masterMac[6] = MASTER_MAC;
static bool espnowReady = false;

static void espnowBegin() {
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();               // never associate: see the note above

  // Pin the channel explicitly rather than inheriting whatever the radio
  // happens to be on. Both ends must agree, and "it worked on the bench"
  // is usually two boards that happened to boot on channel 1.
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(ESPNOW_CHANNEL, WIFI_SECOND_CHAN_NONE);
  esp_wifi_set_promiscuous(false);

  if (esp_now_init() != ESP_OK) {
    Serial.println("# ESP-NOW init failed - this node cannot report");
    return;
  }

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, masterMac, 6);
  peer.channel = ESPNOW_CHANNEL;
  peer.encrypt = false;
  if (esp_now_add_peer(&peer) != ESP_OK) {
    Serial.println("# ESP-NOW could not add the master as a peer");
    return;
  }

  espnowReady = true;
  Serial.printf("# ESP-NOW ready, master %02X:%02X:%02X:%02X:%02X:%02X, channel %d\n",
                masterMac[0], masterMac[1], masterMac[2],
                masterMac[3], masterMac[4], masterMac[5], ESPNOW_CHANNEL);
}

static void espnowSend(const char *kind, float value, const char *unit, const char *severity) {
  if (!espnowReady) {
    Serial.println("# ESP-NOW not ready - reading dropped");
    return;
  }

  SensorPacket pkt = {};          // zeroed, so unused fields are empty strings
  strncpy(pkt.kind, kind, sizeof(pkt.kind) - 1);
  pkt.value = value;
  if (unit)     strncpy(pkt.unit, unit, sizeof(pkt.unit) - 1);
  if (severity) strncpy(pkt.severity, severity, sizeof(pkt.severity) - 1);

  esp_err_t err = esp_now_send(masterMac, (uint8_t *)&pkt, sizeof(pkt));
  // Worth printing: ESP-NOW has no acknowledgement the caller can see
  // here, so this is the only evidence the packet left the board at all.
  Serial.printf("ESP-NOW %s %s -> %s\n", kind,
                (severity && *severity) ? severity : "reading",
                err == ESP_OK ? "sent" : "FAILED");
}
#endif


void reportAlert(const char *kind, const char *severity, const char *message) {
#ifdef REPORT_VIA_ESPNOW
  // The master forwards this as an ALERT line because severity is set.
  // The message text is not carried: the packet is fixed-size and the
  // Pi supplies wording from the kind and severity it already knows.
  (void)message;
  espnowSend(kind, 0.0f, "", severity);
  return;
#endif
  StaticJsonDocument<256> body;
  body["kind"] = kind;
  body["severity"] = severity;
  body["message"] = message;
  body["source"] = "esp32-main";
  String payload;
  serializeJson(body, payload);
  postJson("/api/gate/alerts", payload);
}

// A raw value, not a pre-decided severity — the backend classifies it
// against whatever threshold is configured on the Alerts page for this
// kind (see backend/alerts.py's evaluate_reading()). Reporting a value for
// a kind with no threshold set is harmless: it's logged as the sensor's
// latest reading and raises nothing. unit is optional and defaults to
// whatever the configured threshold already says, if one exists.
void reportReading(const char *kind, float value, const char *unit = nullptr) {
#ifdef REPORT_VIA_ESPNOW
  // Severity left empty on purpose: the Pi holds the site's thresholds
  // from its roster sync, so the same ppm means the same thing whether
  // it arrived this way or over HTTP.
  espnowSend(kind, value, unit ? unit : "", "");
  return;
#endif
  StaticJsonDocument<192> body;
  body["kind"] = kind;
  body["value"] = value;
  if (unit != nullptr) body["unit"] = unit;
  body["source"] = "esp32-main";
  String payload;
  serializeJson(body, payload);
  postJson("/api/gate/sensors", payload);
}

void printStatus() {
  Serial.print("WiFi: ");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "not connected");
  Serial.print("Signed in: ");
  Serial.println(authToken.isEmpty() ? "no" : "yes");
  Serial.print("Auto reporting: ");
  Serial.println(autoMode ? "on" : "off");
}

// ---- real sensor telemetry ----
void sendSensorTelemetry() {
  // analogReadMilliVolts applies the chip's own eFuse calibration, which
  // is more accurate than converting a raw ADC count by hand — and gives
  // a number that means the same thing across different C3 boards, where
  // a raw count wouldn't.
  int gasMv = analogReadMilliVolts(MQ9_PIN);
  Serial.printf("[sensor] gas=%dmV\n", gasMv);
  reportReading("gas", gasMv, "mV");

  float h = dht.readHumidity();
  float t = dht.readTemperature();
  if (isnan(h) || isnan(t)) {
    // DHT11 misses a read occasionally even when wired correctly — not
    // worth alarming over, just skip this tick and try again on the next
    // one rather than reporting a garbage value.
    Serial.println("[sensor] DHT11 read failed, skipping this cycle");
  } else {
    Serial.printf("[sensor] temperature=%.1fC  humidity=%.0f%%\n", t, h);
    reportReading("temperature", t, "C");
    reportReading("humidity", h, "%");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nSafetyFirst ESP32 sensor node");

  // 11dB attenuation gives roughly a 0-2500mV usable input range, which
  // is what the MQ-9's divider is built to output — explicit here rather
  // than trusting whatever a given core version defaults to.
  analogSetPinAttenuation(MQ9_PIN, ADC_11db);
  dht.begin();

  // No cert pinning — this is a demo device signing in with our own
  // account, not something that needs to survive a MITM audit. Only
  // matters at all once API_BASE is an https:// URL; beginRequest() never
  // touches this client for a plain http:// one.
  secureClient.setInsecure();

#ifdef REPORT_VIA_ESPNOW
  // No WiFi join, no sign-in, no credentials on this board at all: it
  // reports to the master, which reaches the Pi over USB. Joining an AP
  // here would move the radio to the router's channel and quietly strand
  // the master, so this path must not touch WiFi.begin().
  Serial.println("Reporting mode: ESP-NOW via the gate master (no WiFi)");
  espnowBegin();
#else
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print(".");
  }
  Serial.print("\nConnected, IP: ");
  Serial.println(WiFi.localIP());

  if (!signIn()) {
    Serial.println("Could not sign in — check API_BASE and that the backend is running and reachable.");
  }
#endif

  Serial.println("\nType a command and press Enter:");
  Serial.println("  gas | smoke | warn        pre-decided severity (/api/gate/alerts)");
  Serial.println("  reading <kind> <value>    raw value, e.g. \"reading gas 450\" (/api/gate/sensors)");
  Serial.println("                            classified against whatever threshold is set");
  Serial.println("                            on the Alerts page for <kind> — configure one there first");
  Serial.println("  auto                      toggle automatic MQ-9 + DHT11 reporting every 4s");
  Serial.println("  status                    reprint WiFi / sign-in state");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    String lower = line;
    lower.toLowerCase();

    if (lower == "gas") {
      reportAlert("gas", "critical", "Manual gas alert (ESP32 serial trigger)");
    } else if (lower == "smoke") {
      reportAlert("smoke", "critical", "Manual smoke alert (ESP32 serial trigger, no sensor wired)");
    } else if (lower == "warn") {
      reportAlert("test", "warning", "Manual warning, non-critical (ESP32 serial trigger)");
    } else if (lower == "auto") {
      autoMode = !autoMode;
      lastAutoSend = 0; // send the first reading immediately, not after a full interval
      Serial.println(autoMode
        ? "Auto reporting ON — reading MQ-9 + DHT11 every 4s"
        : "Auto reporting OFF");
    } else if (lower == "status") {
      printStatus();
    } else if (lower.startsWith("reading ")) {
      // "reading <kind> <value>" — kind can't contain spaces, so the last
      // token is the value and everything between "reading " and it is kind.
      int lastSpace = line.lastIndexOf(' ');
      String kind = line.substring(8, lastSpace);
      String valueStr = line.substring(lastSpace + 1);
      kind.trim();
      if (kind.length() == 0 || valueStr.length() == 0) {
        Serial.println("Usage: reading <kind> <value>, e.g. reading gas 450");
      } else {
        reportReading(kind.c_str(), valueStr.toFloat());
      }
    } else if (lower.length()) {
      Serial.println("Unknown command. Try: gas | smoke | warn | reading <kind> <value> | auto | status");
    }
  }

  if (autoMode && millis() - lastAutoSend >= AUTO_INTERVAL_MS) {
    lastAutoSend = millis();
    sendSensorTelemetry();
  }
}
