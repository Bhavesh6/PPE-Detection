/*
  SafetyFirst — gate master (ESP32, USB-wired to the Raspberry Pi)

  Reads badges from an RC522 and forwards them to the Pi over USB serial.
  Also listens for ESP-NOW packets from the sensor nodes and forwards those
  down the same wire, so the Pi has one input to read instead of several.

  Line protocol — one message per line, verb first. This is what
  pi_app/serial_bridge.py parses; anything else is ignored, so ordinary
  debug printing is harmless.

      BADGE 0006238412
      ALERT gas critical fumes near the mixer
      READING gas 450 ppm

  Wiring (RC522 -> ESP32 38-pin), the VSPI defaults:

      3.3V -> 3V3       SDA/SS -> GPIO5     SCK  -> GPIO18
      GND  -> GND       MOSI   -> GPIO23    MISO -> GPIO19
                        RST    -> GPIO22    IRQ  -> unused

  RC522 runs at 3.3V. It is not 5V tolerant.

  Library: "MFRC522" by GithubCommunity (Arduino Library Manager).

  BADGE FORMAT — the important bit. The Pi's Python reader (SimpleMFRC522)
  turns a card UID into a decimal integer from the first four UID bytes,
  big-endian, and the worker records were created from those values (e.g.
  0006238412). This sketch reproduces that exactly, zero-padded to ten
  digits. Printing hex, or all seven bytes of a 7-byte UID, yields a string
  that matches no worker and reads at the gate as "badge not recognised" —
  a wrong answer that looks like a correct one.
*/

#include <SPI.h>
#include <MFRC522.h>
#include <WiFi.h>
#include <esp_now.h>

#define RC522_SS   5
#define RC522_RST  22

MFRC522 rfid(RC522_SS, RC522_RST);

// Same card ignored for this long after a read. Long enough that holding a
// badge against the reader is one scan, short enough that someone turned
// away for missing gear can fix it and present the same card again.
const unsigned long REPEAT_LOCKOUT_MS = 3000;

String lastUid = "";
unsigned long lastUidAt = 0;

/* What the sensor nodes send. Kept small and fixed-size: ESP-NOW carries at
   most 250 bytes and has no fragmentation. */
typedef struct {
  char  kind[16];      // "gas", "smoke", ...
  float value;         // raw reading
  char  unit[8];       // "ppm", "mV", "" if unitless
  char  severity[10];  // "" to let the Pi classify from the site thresholds
} SensorPacket;

/* First four UID bytes as a big-endian uint32, zero-padded to 10 digits —
   see the note at the top of this file. */
String uidToTag(const MFRC522::Uid &uid) {
  uint32_t value = 0;
  for (byte i = 0; i < 4 && i < uid.size; i++) {
    value = (value << 8) | uid.uidByte[i];
  }
  char buf[11];
  snprintf(buf, sizeof(buf), "%010lu", (unsigned long)value);
  return String(buf);
}

void onEspNowRecv(const esp_now_recv_info_t *info, const uint8_t *data, int len) {
  if (len != sizeof(SensorPacket)) {
    return;                       // not ours, or a version mismatch
  }
  SensorPacket pkt;
  memcpy(&pkt, data, sizeof(pkt));

  // Guarantee termination: a node that filled a field to the brim would
  // otherwise run past it and print whatever follows in memory.
  pkt.kind[sizeof(pkt.kind) - 1] = '\0';
  pkt.unit[sizeof(pkt.unit) - 1] = '\0';
  pkt.severity[sizeof(pkt.severity) - 1] = '\0';

  if (strlen(pkt.severity) > 0) {
    // The node already decided. Used for things it knows are critical
    // regardless of any threshold, like a hardware fault.
    Serial.printf("ALERT %s %s reported by sensor node\n", pkt.kind, pkt.severity);
  } else {
    // Raw value: the Pi classifies it against the site's configured
    // thresholds, so the same reading means the same thing whether it
    // arrives here or over HTTP.
    Serial.printf("READING %s %.2f %s\n", pkt.kind, pkt.value, pkt.unit);
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);

  SPI.begin();                    // SCK 18, MISO 19, MOSI 23
  rfid.PCD_Init();
  delay(50);

  // Not a protocol line, so the Pi ignores it — but it tells a human with a
  // serial monitor that the reader is actually alive.
  byte version = rfid.PCD_ReadRegister(MFRC522::VersionReg);
  if (version == 0x00 || version == 0xFF) {
    Serial.println("# RC522 not responding - check wiring and that it is on 3.3V");
  } else {
    Serial.printf("# RC522 ready (version 0x%02X)\n", version);
  }

  // ESP-NOW needs the radio in station mode, but the master does not join a
  // network: it reaches the Pi over USB. Staying off Wi-Fi also avoids the
  // channel trap, where joining an AP moves the radio and silently strands
  // peers pinned to a different channel.
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();

  if (esp_now_init() == ESP_OK) {
    esp_now_register_recv_cb(onEspNowRecv);
    Serial.printf("# ESP-NOW ready, mac %s, channel %d\n",
                  WiFi.macAddress().c_str(), WiFi.channel());
  } else {
    Serial.println("# ESP-NOW init failed - badges will still work");
  }

  Serial.println("# gate master ready");
}

void loop() {
  if (!rfid.PICC_IsNewCardPresent() || !rfid.PICC_ReadCardSerial()) {
    delay(50);
    return;
  }

  String tag = uidToTag(rfid.uid);
  unsigned long now = millis();

  // Dedupe on time as well as identity: keying on "different from last"
  // alone means the same badge can never be read twice in a row, so a
  // worker who fixes their gear and re-presents it is met with silence.
  if (tag != lastUid || now - lastUidAt >= REPEAT_LOCKOUT_MS) {
    Serial.print("BADGE ");
    Serial.println(tag);
    lastUid = tag;
    lastUidAt = now;
  }

  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
  delay(50);
}
