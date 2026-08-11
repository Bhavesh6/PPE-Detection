# ESP32 connectivity test

Not the real sensor board — a throwaway sketch for answering one question
before the real hardware exists: **can an ESP32 reach this PC and talk to
the backend at all?** Useful right now because the ESP32 is on hand but the
Raspberry Pi isn't yet, so this tests the network/auth path independently
of the Pi.

`alert_sim/alert_sim.ino` signs in the same way `pi_app/checkpoint.py`
does, then waits for a command typed into the Arduino IDE's Serial Monitor
and reports either a pre-decided alert (`/api/gate/alerts`) or a raw sensor
value (`/api/gate/sensors`, classified against a threshold you configure on
the Alerts page) — the same two endpoints the real ESP32-main sensor board
will use once it's built. Nothing on the backend changes on that day; this
sketch is disposable, the endpoints aren't.

## Setup

1. Arduino IDE → Boards Manager → install **esp32** (by Espressif Systems).
2. Library Manager → install **ArduinoJson** (by Benoit Blanchon, v6.x).
3. Open `alert_sim/alert_sim.ino` and fill in `WIFI_SSID`, `WIFI_PASSWORD`,
   and `API_BASE`.

   `API_BASE` is your PC's **LAN IP**, not `localhost` — the ESP32 is a
   separate device on the network. Find it with `ipconfig` (Windows) or
   `ip addr` (Linux) on the machine running `python app.py`, e.g.
   `http://192.168.1.42:5000`. Both the PC and the ESP32 need to be on the
   same WiFi network, and the backend must be running (`cd backend &&
   python app.py`) and bound to `0.0.0.0` (it already is, by default).

   `DEVICE_EMAIL`/`DEVICE_PASSWORD` are optional — leave both blank to sign
   in as a guest, which is enough for this test since `/api/gate/alerts`
   only needs *any* signed-in session, not an admin one.

4. Tools → Board → your ESP32 model, then Upload.
5. Tools → Serial Monitor, **115200 baud**, line ending set to **Newline**.

## What it proves

Type one of these into the Serial Monitor and press Enter:

```
gas                     critical gas alert — holds the gate, per detection.evaluate_access()
smoke                   critical smoke alert
warn                    a non-critical warning — heads-up only, doesn't hold the gate
reading <kind> <value>  a raw value, e.g. "reading gas 450" — set a threshold for
                        <kind> on the Alerts page first, or this just logs quietly
auto                    toggle continuous simulated telemetry (see below)
status                  reprint WiFi / sign-in state
```

The `reading` command is the more realistic one for how the real board will
actually behave — reporting numbers, not deciding severity itself. Configure
a threshold on the Alerts page (e.g. gas: warning at 400, critical at 800,
unit ppm, direction "above"), then send `reading gas 450` for a warning and
`reading gas 900` for a critical, and watch the same gate-holding/popup
behavior trigger from a number instead of a hardcoded severity string.

## Continuous telemetry (`auto`)

Typing individual `reading` commands proves the endpoint works, but it
doesn't look or feel like a sensor. Send `auto` and the sketch instead
posts a simulated gas ppm and temperature reading every 4 seconds on its
own, each drifting from the last with a small random step (a real sensor's
value doesn't jump around independently frame to frame) and, roughly once
every couple of minutes, a larger random spike — so the gas value
occasionally crosses into warning or critical territory on its own instead
of only when you type a number.

To watch it: open the Alerts page and leave `auto` running. The **Live
Readings** panel updates with both values as they drift, gas trips a
warning or critical banner on its own when the random walk crosses your
configured threshold (400 / 800 ppm by default in this project's setup),
and the Serial Monitor prints the same numbers it's sending so you can
compare what the ESP32 thinks it sent against what shows up in the
console. Send `auto` again to stop it.

Temperature has no threshold configured by default, so it only ever
appears in Live Readings and never raises an alert — a look at the "a
sensor kind with no threshold set just logs" path from the other side of
the wire, not just the admin UI.

Then check the web admin console's **Alerts** page (or any open admin/
operator tab — a critical one pops up as a banner everywhere) — if it shows
up there, the ESP32 can reach the backend and authenticate correctly, which
is the whole point of this test. If it doesn't show up, `status` will say
whether the problem is WiFi (not connected) or the backend (signed in
false) — check `API_BASE` and that `python app.py` is actually running and
reachable from another device on the same network before assuming the
ESP32 side is broken.

## What this isn't

Not a sensor driver, not the real ESP32-main firmware, no actual gas/smoke
hardware involved. When the real board and sensors exist, this file gets
replaced, not extended — the useful part it's proving (the network path and
auth flow work) doesn't need re-testing once real hardware confirms the
same thing with real readings.
