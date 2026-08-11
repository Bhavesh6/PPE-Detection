/*
  ESP32 alert simulator — for testing ESP32 <-> backend connectivity before
  the real sensor board (ESP32-main) exists, and before the Pi is on hand.

  It doesn't read any sensor. It signs in like any other device, then waits
  for a line typed into the Arduino IDE's Serial Monitor and reports either
  a pre-decided alert or a raw sensor value — hitting the exact same
  /api/gate/alerts and /api/gate/sensors endpoints the real ESP32-main
  board will use once it's built (see pi_app/checkpoint.py's ApiClient for
  the Pi-side equivalent of this same sign-in-then-POST pattern). Nothing
  on the backend needs to change when the real board arrives; this sketch
  is disposable, the endpoints aren't.

  Serial Monitor commands (115200 baud, newline line ending):
    gas                     critical gas alert
    smoke                   critical smoke alert
    warn                    a non-critical warning (doesn't hold the gate)
    reading <kind> <value>  a raw value, e.g. "reading gas 450" — classified
                            against whatever threshold is configured for
                            <kind> on the Alerts page; no threshold set for
                            that kind means it's just logged, nothing fires
    auto                    toggle continuous simulated telemetry: gas and
                            temperature readings sent every few seconds,
                            drifting around a baseline with occasional
                            random spikes — so a threshold trips on its own
                            during a demo instead of only on typed commands
    status                  reprint WiFi/sign-in state

  Setup:
    1. Arduino IDE -> Boards Manager -> install "esp32" (Espressif Systems).
    2. Library Manager -> install "ArduinoJson" (by Benoit Blanchon, v6.x).
    3. Fill in the constants below.
    4. Tools -> Board -> your ESP32 board, then Upload.
    5. Tools -> Serial Monitor, 115200 baud, line ending "Newline".

  API_BASE must be your PC's LAN IP (e.g. http://192.168.1.42:5000), not
  localhost or 127.0.0.1 — the ESP32 is a separate device on the network,
  not the machine running the backend. Both need to be on the same WiFi.
  Find the IP with `ipconfig` (Windows) or `ip addr` (Linux/Pi) on the PC
  running `python app.py`.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// WIFI_SSID, WIFI_PASSWORD, API_BASE, DEVICE_EMAIL, DEVICE_PASSWORD live in
// secrets.h, next to this file — gitignored, so a real WiFi password or
// device login never ends up in the tracked .ino the way it briefly did
// here. Copy secrets.h.example to secrets.h and fill in real values; the
// Arduino IDE picks up a same-folder header automatically, no include path
// setup needed.
#include "secrets.h"

String authToken;

// Simulated continuous telemetry state — declared here, ahead of every
// function, because C++ doesn't hoist globals the way the loop()/setup()
// style might suggest: a function defined earlier in the file can't see a
// variable declared later, only a forward-declared or already-seen one.
bool autoMode = false;
unsigned long lastAutoSend = 0;
const unsigned long AUTO_INTERVAL_MS = 4000;
float simGas = 150.0;   // ppm — baseline chosen well under the 400/800
                        // warning/critical thresholds this project's demo
                        // has configured on the Alerts page
float simTemp = 28.0;   // deg C — ambient, no threshold configured for this
                        // kind by default, so it only ever logs quietly

bool signIn() {
  HTTPClient http;
  bool useCredentials = strlen(DEVICE_EMAIL) > 0 && strlen(DEVICE_PASSWORD) > 0;
  String url = String(API_BASE) + (useCredentials ? "/api/auth/login" : "/api/auth/guest");
  http.begin(url);
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
  http.begin(String(API_BASE) + path);
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + authToken);
  int code = http.POST(payload);

  // A 401 means the token expired or the backend restarted (in-memory JWT
  // secret changed) — one retry after a fresh sign-in covers both without
  // needing a reboot.
  if (code == 401 && signIn()) {
    http.end();
    http.begin(String(API_BASE) + path);
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", "Bearer " + authToken);
    code = http.POST(payload);
  }

  Serial.printf("POST %s -> HTTP %d\n", path, code);
  if (code > 0) Serial.println(http.getString());
  http.end();
  return code;
}

void reportAlert(const char *kind, const char *severity, const char *message) {
  StaticJsonDocument<256> body;
  body["kind"] = kind;
  body["severity"] = severity;
  body["message"] = message;
  body["source"] = "esp32-sim";
  String payload;
  serializeJson(body, payload);
  postJson("/api/gate/alerts", payload);
}

// A raw value, not a pre-decided severity — the backend classifies it
// against whatever threshold is configured on the Alerts page for this
// kind (see backend/alerts.py's evaluate_reading()). Reporting a value for
// a kind with no threshold set is harmless: it's logged as the sensor's
// latest reading and raises nothing.
void reportReading(const char *kind, float value) {
  StaticJsonDocument<192> body;
  body["kind"] = kind;
  body["value"] = value;
  body["source"] = "esp32-sim";
  String payload;
  serializeJson(body, payload);
  postJson("/api/gate/sensors", payload);
}

void printStatus() {
  Serial.print("WiFi: ");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "not connected");
  Serial.print("Signed in: ");
  Serial.println(authToken.isEmpty() ? "no" : "yes");
  Serial.print("Auto telemetry: ");
  Serial.println(autoMode ? "on" : "off");
}

// ---- simulated continuous telemetry ----
// No real sensor exists yet — this stands in for one until ESP32-main and
// actual hardware arrive. A pure random value per tick would look fake and
// would never organically cross a threshold twice in a row; a random walk
// (small step each tick, gently pulled back toward a baseline) drifts the
// way a real reading does, and an occasional larger jump gives the alert
// system something real to catch without waiting on an actual leak.
// (State variables live near authToken, above — see the comment there.)

float driftValue(float current, float baseline, float noise, float minV, float maxV) {
  float pulled = current + (baseline - current) * 0.05;      // gentle pull home
  float step = (random(-100, 101) / 100.0) * noise;           // small random step
  float next = pulled + step;
  if (next < minV) next = minV;
  if (next > maxV) next = maxV;
  return next;
}

void sendSimulatedTelemetry() {
  // Roughly once every couple of minutes on average (1-in-40 chance per
  // 4s tick), not on a fixed schedule — a predictable spike would look
  // scripted, not sensed.
  if (random(0, 40) == 0) {
    simGas += random(350, 650);
  }

  simGas = driftValue(simGas, 150.0, 15.0, 0, 1200);
  simTemp = driftValue(simTemp, 28.0, 0.6, 15, 55);

  Serial.printf("[auto] gas=%.0fppm  temperature=%.1fC\n", simGas, simTemp);
  reportReading("gas", simGas);
  reportReading("temperature", simTemp);
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nSafetyFirst ESP32 alert simulator");

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

  // An unconnected analog pin floats on real noise — good enough entropy
  // for demo telemetry, and this board has no real sensor to seed from.
  randomSeed(analogRead(0) + millis());

  Serial.println("\nType a command and press Enter:");
  Serial.println("  gas | smoke | warn        pre-decided severity (/api/gate/alerts)");
  Serial.println("  reading <kind> <value>    raw value, e.g. \"reading gas 450\" (/api/gate/sensors)");
  Serial.println("                            classified against whatever threshold is set");
  Serial.println("                            on the Alerts page for <kind> — configure one there first");
  Serial.println("  auto                      toggle continuous simulated gas + temperature telemetry");
  Serial.println("  status                    reprint WiFi / sign-in state");
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();

    String lower = line;
    lower.toLowerCase();

    if (lower == "gas") {
      reportAlert("gas", "critical", "Simulated gas leak (ESP32 serial trigger)");
    } else if (lower == "smoke") {
      reportAlert("smoke", "critical", "Simulated smoke detection (ESP32 serial trigger)");
    } else if (lower == "warn") {
      reportAlert("test", "warning", "Simulated warning, non-critical (ESP32 serial trigger)");
    } else if (lower == "auto") {
      autoMode = !autoMode;
      lastAutoSend = 0; // send the first reading immediately, not after a full interval
      Serial.println(autoMode
        ? "Auto telemetry ON — sending simulated gas + temperature every 4s"
        : "Auto telemetry OFF");
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
    sendSimulatedTelemetry();
  }
}
