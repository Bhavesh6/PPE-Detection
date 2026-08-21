# ESP32 boards

Five boards, four sketches. Every one of them reaches the Raspberry Pi, or
reaches something that does — the Pi is the only box on site with a route
out.

| Board | Sketch | Board file | Hardware |
|---|---|---|---|
| Gate master | [`gate_master/`](gate_master/) | — | ESP32 38-pin · RC522 · fan PWM/tach |
| Sensor node — gate | [`ppe_sensors/`](ppe_sensors/) | `board_gate.h` | ESP32-C3 SuperMini · MQ-9 · DHT11 |
| Sensor node — yard | [`ppe_sensors/`](ppe_sensors/) | `board_yard.h` | ESP32-C3 SuperMini · MQ-9 · DHT11 |
| Camera — gate | [`cctv_cam/`](cctv_cam/) | `board_cam.h` | ESP32-CAM · **OV2640** |
| Camera — yard | [`cctv_cam/`](cctv_cam/) | `board_yard.h` | ESP32-CAM · **GC2145** |

[`esp32-sim/`](../esp32-sim/) is a fifth sketch with no board of its own —
a hardware-free stand-in that signs in identically and reports typed
values, kept so the alert pipeline can still be demonstrated if a node's
wiring fails at the venue.

## One sketch, several boards

Boards that share a sketch differ only by a tracked `board_*.h` file, and
`secrets.h` picks which one:

```c
#include "board_yard.h"        // <- the only line that changes
#define WIFI_SSID "..."
#define WIFI_PASSWORD "..."
```

The board files are in git on purpose. Nothing in them is secret, and
they are the repo's only record that a second camera and a second sensor
node exist at all — `secrets.h` is gitignored, so anything hidden in
there is invisible to everyone who clones this.

The alternative — a folder per board — means the same fix applied four
times, and the ESP-NOW packet struct has to stay byte-identical across
files or the master silently drops every packet.

## Identity is not cosmetic

Both pairs must carry distinct ids, and for the same underlying reason:
**the backend keeps one live row per key.**

- Two cameras sharing a `CAM_ID` overwrite each other's frames, and the
  console shows one tile flickering between two places.
- Two sensor nodes sharing a `NODE_ID` overwrite each other's readings,
  and the console shows whichever node spoke last.

Naming the sensor nodes buys something beyond the fix: thresholds are
configured per kind, so `gate_gas` and `yard_gas` get their own limits. An
open yard disperses gas far faster than an enclosed gate area, so the same
number does not mean the same thing in both places.

## The two cameras are not the same silicon

The OV2640 has a hardware JPEG encoder. The **GC2145** — sold as
RHYX-M21-45 or M12-45 — has none at all, and emits RGB565/YUV/RAW only.

One firmware covers both: it initialises optimistically in JPEG mode,
catches the refusal, and re-initialises in RGB565 with software encoding
at QVGA. Serial says which path it took, and that line is the first thing
to read when a picture is slow or missing:

```
[cam] JPEG mode refused (0x106) - retrying as RGB565
[cam] sensor: GC2145 (RHYX-M21-45) (PID 0x2145)
[cam] software (slower) JPEG, PSRAM found
```

Because software encoding is slower, poll that board less often — on the
Pi, `SAFETYFIRST_CCTV_INTERVAL_YARD=3.0`.

## Reflash the master and the nodes together

The ESP-NOW packet struct is duplicated in `gate_master.ino` and
`ppe_sensors.ino`, and the master drops any packet whose length does not
match `sizeof(SensorPacket)` exactly. A node left on an older struct goes
**silent rather than erroring** — which looks like dead hardware, not a
version mismatch. If you change that struct, flash everything.

## Sensor node wiring

**MQ-9** — `VCC`→5V, `GND`→GND, `AOUT`→10kΩ→**node**→10kΩ→GND,
**node**→`GPIO0`. The divider halves AOUT's roughly 5V swing to a safe
~2.5V for the C3's ADC. `DOUT` unused.

**DHT11** — `VCC`→5V, `GND`→GND, `DATA`→`GPIO4`. A bare 4-pin sensor also
needs a 10kΩ pull-up from `DATA` to 5V; the 3-pin breakout has one built in.

Avoid `GPIO8` (onboard LED) and `GPIO9` (boot strapping) for anything else.

### Gas is in millivolts, not ppm

A raw MQ-9 has no ppm without calibrating its Rs/R0 ratio against a known
gas concentration — equipment nobody has at a hackathon. Reporting the
honest unit beats fabricating a precise-looking number that isn't real.

**Before the demo:** watch a few minutes of clean-air baseline in Serial,
then set the threshold for `gate_gas` (and `yard_gas`) above it on the
Alerts page, **with the unit changed to `mV`**. A threshold left in `ppm`
from simulator testing will either never fire or fire immediately.

`smoke` has no sensor wired — the Serial command is a manual trigger only.

## Setup

1. Arduino IDE → Boards Manager → **esp32** (Espressif).
2. Libraries: **ArduinoJson** (v6.x) and **DHT sensor library** (Adafruit,
   accept the Unified Sensor dependency) for `ppe_sensors`. The camera and
   master sketches need nothing extra.
3. `cp secrets.h.example secrets.h`, set the `#include` to the right board
   file, fill in WiFi.
4. Board: **ESP32C3 Dev Module** for sensor nodes (set *USB CDC On Boot →
   Enabled* on a SuperMini, or Serial Monitor stays blank); **AI Thinker
   ESP32-CAM** for cameras, with Partition Scheme **Huge APP (3MB No OTA)**.
5. Serial Monitor at 115200.
