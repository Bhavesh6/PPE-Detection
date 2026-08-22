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
      FAN 62 2480                 duty percent, measured RPM

  The wire runs both ways now. From the Pi:

      TEMP 54.8 1.35              CPU temperature °C, load average
      FAN 70                      pin the fan at a duty percent
      FAN AUTO                    hand it back to the curve

  Anything else in either direction is ignored, so ordinary debug
  printing stays harmless on both sides.

  Wiring (RC522 -> ESP32 38-pin), the VSPI defaults:

      3.3V -> 3V3       SDA/SS -> GPIO5     SCK  -> GPIO18
      GND  -> GND       MOSI   -> GPIO23    MISO -> GPIO19
                        RST    -> GPIO22    IRQ  -> unused

  Cooling fan (4-pin), on the same board because the AI HAT has no
  temperature sensor and the Pi's own fan header is occupied:

      PWM  -> GPIO25    25kHz control
      TACH -> GPIO26    open-collector, pulled up internally

  The fan's supply and the ESP32 must share a ground, or the tach reads
  nothing and the PWM does very little.

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
#include <esp_mac.h>   // esp_read_mac

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
/* Widened from 16 to 24 so a node can qualify its readings with its own
   name — "yard_temperature" is 16 characters and would have been
   truncated. Both sides must be reflashed together: the length check
   below drops anything that does not match exactly, so a node still
   running the old struct goes silent rather than erroring. */
typedef struct {
  char  kind[24];      // "gas", "yard_temperature", ...
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

/* ---- Cooling fan ----------------------------------------------------

   This board also drives the Pi's fan, because the AI HAT has no
   temperature sensor of its own and the Pi's own header is occupied. The
   Pi sends its temperature and load down the same USB line the badges
   come back on; this decides a duty cycle and reports the measured RPM.

       Pi  -> master:   TEMP 54.8 1.35
       master -> Pi:    FAN 62 2480

   Why the curve lives here rather than on the Pi: the dangerous failure
   is a fan that stops while the Pi cooks. If the decision were made on
   the Pi, a crashed or busy Pi would simply stop asking for airflow and
   the fan would idle at whatever it was last told - exactly when it is
   least safe. Here, silence is treated as a fault: after FAILSAFE_MS
   with no update the fan goes to FAILSAFE_DUTY rather than staying put,
   because an unreachable Pi is more likely to be a hot one than a cold
   one.

   Wiring (4-pin fan):
       PWM  -> GPIO25      control, 25kHz
       TACH -> GPIO26      open-collector, needs a pull-up
       12V/5V and GND from the fan's own supply, grounds tied together
*/
#define FAN_PWM_PIN   25
#define FAN_TACH_PIN  26

// 25kHz is the Intel 4-wire fan spec. Below ~20kHz the fan whines
// audibly, which on a gate at head height is worth avoiding.
#define FAN_PWM_FREQ  25000
#define FAN_PWM_BITS  8

// Never fully stop. Many fans will not restart from 0% without a kick,
// and a gate box with no airflow at all is worse than a quiet hum.
#define FAN_MIN_DUTY  25
#define FAN_MAX_DUTY  100

// The curve, in °C. A Pi 5 begins throttling around 80, so full speed is
// reached well before that rather than as a reaction to it.
#define FAN_TEMP_MIN  45.0f       // at or below: minimum duty
#define FAN_TEMP_MAX  75.0f       // at or above: full duty

// No word from the Pi for this long and we assume the worst.
#define FAILSAFE_MS   30000UL
#define FAILSAFE_DUTY 80

// Most 4-pin fans emit two tach pulses per revolution.
#define TACH_PULSES_PER_REV 2
#define FAN_REPORT_MS 5000UL

static volatile unsigned long tachPulses = 0;
static unsigned long lastTachRead = 0;
static unsigned long lastTempAt = 0;
static unsigned long lastFanReport = 0;
static int  fanDuty = FAILSAFE_DUTY;   // start cooling before we are told anything
static int  fanRpm = 0;
static bool fanManual = false;         // an operator pinned it; skip the curve

void IRAM_ATTR onTachPulse() {
  tachPulses++;
}

static void applyDuty(int duty) {
  if (duty < FAN_MIN_DUTY) duty = FAN_MIN_DUTY;
  if (duty > FAN_MAX_DUTY) duty = FAN_MAX_DUTY;
  fanDuty = duty;
  ledcWrite(FAN_PWM_PIN, (duty * ((1 << FAN_PWM_BITS) - 1)) / 100);
}

/* Temperature sets the floor; heavy load raises it early.

   Load is used as a leading indicator, not a second thermometer: a Pi
   that has just started inference is already producing the heat that its
   sensor will report in twenty seconds. Spinning up now is cheaper than
   catching up later, and the fan is the only thing that can act early. */
static int dutyFor(float tempC, float load) {
  int fromTemp;
  if (tempC <= FAN_TEMP_MIN) {
    fromTemp = FAN_MIN_DUTY;
  } else if (tempC >= FAN_TEMP_MAX) {
    fromTemp = FAN_MAX_DUTY;
  } else {
    float span = (tempC - FAN_TEMP_MIN) / (FAN_TEMP_MAX - FAN_TEMP_MIN);
    fromTemp = FAN_MIN_DUTY + (int)(span * (FAN_MAX_DUTY - FAN_MIN_DUTY));
  }

  // The Pi 5 has four cores, so a load average near 4 is fully busy.
  int fromLoad = 0;
  if (load >= 4.0f)      fromLoad = 80;
  else if (load >= 3.0f) fromLoad = 65;
  else if (load >= 2.0f) fromLoad = 50;

  return fromTemp > fromLoad ? fromTemp : fromLoad;
}

/* Lines from the Pi. Unknown input is ignored rather than answered, so
   the Pi can print whatever it likes on this wire without confusing us —
   the same courtesy the Pi extends to our own '#' comments. */
static void handleSerialLine(const String &line) {
  if (line.startsWith("TEMP ")) {
    float temp = 0, load = 0;
    // Load is optional: a Pi that cannot read it still gets cooled.
    int parsed = sscanf(line.c_str() + 5, "%f %f", &temp, &load);
    if (parsed >= 1) {
      lastTempAt = millis();
      if (!fanManual) applyDuty(dutyFor(temp, load));
    }
  } else if (line.startsWith("FAN ")) {
    String arg = line.substring(4);
    arg.trim();
    if (arg.equalsIgnoreCase("AUTO")) {
      fanManual = false;
      Serial.println("# fan back to automatic");
    } else {
      fanManual = true;
      applyDuty(arg.toInt());
      Serial.printf("# fan pinned at %d%%\n", fanDuty);
    }
  }
}

static void readSerial() {
  static String buf;
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (buf.length()) {
        handleSerialLine(buf);
        buf = "";
      }
    } else if (buf.length() < 80) {
      buf += ch;
    }
  }
}

static void serviceFan() {
  unsigned long now = millis();

  // Silence from the Pi is a fault, not a reason to coast. Only overrides
  // the automatic curve — an operator who pinned a speed keeps it.
  if (!fanManual && lastTempAt && (now - lastTempAt > FAILSAFE_MS) && fanDuty < FAILSAFE_DUTY) {
    applyDuty(FAILSAFE_DUTY);
    Serial.println("# no temperature from the Pi - fan to failsafe");
  }

  if (now - lastTachRead >= 1000) {
    noInterrupts();
    unsigned long pulses = tachPulses;
    tachPulses = 0;
    interrupts();
    unsigned long elapsed = now - lastTachRead;
    lastTachRead = now;
    // pulses per second -> revolutions per minute
    fanRpm = (int)((pulses * 60000UL) / (elapsed * TACH_PULSES_PER_REV));
  }

  if (now - lastFanReport >= FAN_REPORT_MS) {
    lastFanReport = now;
    Serial.printf("FAN %d %d\n", fanDuty, fanRpm);
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
    // Read the address out of eFuse rather than asking WiFi.macAddress().
    // That call answers 00:00:00:00:00:00 until the Wi-Fi driver has
    // finished coming up, and on this board it has not got there by the
    // time setup() prints - a clean boot on the bench reported all zeros.
    //
    // The address is the whole point of this line: it is what each sensor
    // node needs for MASTER_MAC, and a node pinned to 00:00:00:00:00:00
    // never reaches anybody. eFuse can be read immediately and does not
    // care what the driver is doing.
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    Serial.printf("# ESP-NOW ready, mac %02X:%02X:%02X:%02X:%02X:%02X, channel %d\n",
                  mac[0], mac[1], mac[2], mac[3], mac[4], mac[5], WiFi.channel());
  } else {
    Serial.println("# ESP-NOW init failed - badges will still work");
  }

  // Fan first, and already turning: this board boots before the Pi has
  // finished starting, and the gap is exactly when nothing is watching
  // the temperature.
  ledcAttach(FAN_PWM_PIN, FAN_PWM_FREQ, FAN_PWM_BITS);
  applyDuty(FAILSAFE_DUTY);
  pinMode(FAN_TACH_PIN, INPUT_PULLUP);   // tach is open-collector
  attachInterrupt(digitalPinToInterrupt(FAN_TACH_PIN), onTachPulse, FALLING);
  lastTachRead = millis();
  Serial.printf("# fan on GPIO%d at %d%%, tach on GPIO%d\n",
                FAN_PWM_PIN, fanDuty, FAN_TACH_PIN);

  Serial.println("# gate master ready");
}

void loop() {
  // Both run every pass, before the early return below: the fan must not
  // stop being serviced just because nobody is presenting a badge, and
  // that early return is why this sits at the top rather than the bottom.
  readSerial();
  serviceFan();

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
