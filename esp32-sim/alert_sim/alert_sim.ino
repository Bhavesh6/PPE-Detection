/*
  ESP32 alert simulator — for testing ESP32 <-> backend connectivity before
  the real sensor board (ESP32-main) exists, and before the Pi is on hand.

  It doesn't read any sensor. It signs in like any other device, then waits
  for a line typed into the Arduino IDE's Serial Monitor and reports a
  simulated alert — hitting the exact same /api/gate/alerts endpoint the
  real ESP32-main board will use once it's built (see pi_app/checkpoint.py's
  ApiClient for the Pi-side equivalent of this same sign-in-then-POST
  pattern). Nothing on the backend needs to change when the real board
  arrives; this sketch is disposable, the endpoint isn't.

  Serial Monitor commands (115200 baud, newline line ending):
    gas         critical gas alert
    smoke       critical smoke alert
    warn        a non-critical warning (heads-up only, doesn't hold the gate)
    status      reprint WiFi/sign-in state

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

// ---- fill these in ----
const char *WIFI_SSID = "YOUR_WIFI_SSID";
const char *WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char *API_BASE = "http://192.168.1.42:5000";

// A device account created via the web sign-up (same as pi_app/.env's
// SAFETYFIRST_EMAIL/PASSWORD). Leave both blank to sign in as a guest
// instead — fine for this connectivity test, since /api/gate/alerts only
// needs *any* signed-in session, not an admin one.
const char *DEVICE_EMAIL = "";
const char *DEVICE_PASSWORD = "";
// ------------------------

String authToken;

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

void reportAlert(const char *kind, const char *severity, const char *message) {
  if (authToken.isEmpty() && !signIn()) {
    Serial.println("Not signed in — cannot report the alert.");
    return;
  }

  HTTPClient http;
  http.begin(String(API_BASE) + "/api/gate/alerts");
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", "Bearer " + authToken);

  StaticJsonDocument<256> body;
  body["kind"] = kind;
  body["severity"] = severity;
  body["message"] = message;
  body["source"] = "esp32-sim";
  String payload;
  serializeJson(body, payload);

  int code = http.POST(payload);
  // A 401 means the token expired or the backend restarted (in-memory JWT
  // secret changed) — one retry after a fresh sign-in covers both without
  // needing a reboot.
  if (code == 401 && signIn()) {
    http.end();
    http.begin(String(API_BASE) + "/api/gate/alerts");
    http.addHeader("Content-Type", "application/json");
    http.addHeader("Authorization", "Bearer " + authToken);
    code = http.POST(payload);
  }

  Serial.printf("POST /api/gate/alerts -> HTTP %d\n", code);
  if (code > 0) Serial.println(http.getString());
  http.end();
}

void printStatus() {
  Serial.print("WiFi: ");
  Serial.println(WiFi.status() == WL_CONNECTED ? WiFi.localIP().toString() : "not connected");
  Serial.print("Signed in: ");
  Serial.println(authToken.isEmpty() ? "no" : "yes");
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

  Serial.println("\nType a command and press Enter: gas | smoke | warn | status");
}

void loop() {
  if (!Serial.available()) return;

  String line = Serial.readStringUntil('\n');
  line.trim();
  line.toLowerCase();

  if (line == "gas") {
    reportAlert("gas", "critical", "Simulated gas leak (ESP32 serial trigger)");
  } else if (line == "smoke") {
    reportAlert("smoke", "critical", "Simulated smoke detection (ESP32 serial trigger)");
  } else if (line == "warn") {
    reportAlert("test", "warning", "Simulated warning, non-critical (ESP32 serial trigger)");
  } else if (line == "status") {
    printStatus();
  } else if (line.length()) {
    Serial.println("Unknown command. Try: gas | smoke | warn | status");
  }
}
