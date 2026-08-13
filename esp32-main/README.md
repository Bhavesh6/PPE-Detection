# ESP32 sensor node (real hardware)

`ppe_sensors/ppe_sensors.ino` reads an actual MQ-9 gas sensor and DHT11
temperature/humidity sensor and reports them to the backend — the real
counterpart to `esp32-sim/alert_sim`, which fakes the same readings for
testing without hardware. Keep `alert_sim` around: if this board's wiring
breaks at the venue, flashing that instead still lets you demo the alert
pipeline with believable numbers, and nothing on the backend needs to know
which one it's talking to.

## Wiring

**MQ-9** — `VCC`→5V, `GND`→GND, `AOUT`→10kΩ→**node**→10kΩ→GND, **node**→`GPIO0`.
The divider halves AOUT's roughly 5V swing to a safe ~2.5V max for the
ESP32-C3's ADC. `DOUT` is unused.

**DHT11** — `VCC`→5V, `GND`→GND, `DATA`→`GPIO4`. If you have the bare 4-pin
sensor (not the 3-pin breakout module), also add a 10kΩ pull-up from
`DATA` to 5V — the breakout module already has one built in.

Avoid `GPIO8` (onboard LED) and `GPIO9` (boot-strapping) for anything else
you wire to this board later.

## Setup

1. Arduino IDE → Boards Manager → install **esp32** (Espressif Systems).
2. Library Manager → install **ArduinoJson** (v6.x) and **DHT sensor
   library** (by Adafruit) — the latter prompts to also install **Adafruit
   Unified Sensor**; accept that too, both are required.
3. Copy `secrets.h.example` to `secrets.h` (same folder) and fill in your
   WiFi and `API_BASE` — gitignored, same pattern as `esp32-sim`.
4. Tools → Board → **ESP32C3 Dev Module**. On a SuperMini board, also set
   **USB CDC On Boot → Enabled**, or Serial Monitor stays blank.
5. Upload, then open Serial Monitor at 115200 baud, type `auto`.

## Gas is in millivolts, not ppm

A raw MQ-9 has no ppm without calibrating its Rs/R0 ratio against a known
gas concentration — equipment nobody doing this at a hackathon has on
hand. Reporting the honest unit (raw millivolts off the divider) beats
fabricating a precise-looking number that isn't real.

**Before the demo:** watch a few minutes of Serial Monitor output in clean
air to see the baseline mV reading, then go to the Alerts page and set the
`gas` threshold above that baseline, with its **unit changed to `mV`**.
The threshold left over from testing with `alert_sim` is in `ppm` and
won't mean anything against a real reading — it'll either never fire or
fire immediately, depending on how the numbers happen to compare.

## What's still simulated

`smoke` has no sensor wired — the `smoke` Serial command still exists but
is a manual trigger only, same as `gas`/`warn`. Nothing about that will
change until a smoke sensor is actually added to the circuit.
