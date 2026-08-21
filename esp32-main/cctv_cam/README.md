# Site CCTV cameras (ESP32-CAM)

Extra eyes on the site, separate from the gate. The checkpoint camera is
busy deciding who gets in; pointing it at the yard would mean choosing
between the two.

**Monitoring only — no PPE detection.** Frames are not run through the
model and raise no records. That was deliberate: the gate's inference is
per-badge and bounded, while continuously analysing extra cameras is a
standing cost on the backend for footage nobody has asked to be ruled on.

```
camera(s) ──LAN──► Pi (cctv_relay) ──existing session──► backend ──► console
   (no auth)          the only box with a backhaul        keys by id
```

## Two sensors, one sketch

These boards ship with different camera modules, and the difference is
not cosmetic:

| Sensor | Marking | JPEG | Runs at |
|---|---|---|---|
| **OV2640** | — | hardware encoder | VGA 640×480 |
| **GC2145** | RHYX-M21-45 / M12-45 | **none** — RGB565/YUV/RAW only | QVGA 320×240, software encoded |

The GC2145 has no JPEG encoder at all, so every frame must be compressed
on the CPU. That is slow, which is why it drops to QVGA — and why people
report lag with these modules.

Rather than two sketches or a `#define` nobody remembers to change, the
firmware **initialises in JPEG mode and falls back to RGB565 with
software encoding if the sensor refuses**. Serial says which sensor was
found and which path is in use:

```
[cam] sensor: GC2145 (RHYX-M21-45) (PID 0x2145)
[cam] software (slower) JPEG, PSRAM found
```

"The picture is slow" and "the picture is missing" have very different
causes on these two parts, so it is worth reading that line before
debugging anything else.

## Naming — required when you have more than one

Each board needs its own `CAM_ID` in `secrets.h`. It becomes both the
mDNS hostname (`safetyfirst-<CAM_ID>.local`) and the key the console
files frames under.

**Two boards sharing an id overwrite each other's frames**, and the
console shows one tile flickering between two places — which looks like
a network fault rather than a naming mistake. Name them after where they
point (`gate`, `yard`, `shaft`), not after the hardware: the id is the
label an operator reads.

## Flashing

No USB port on these boards — you need a USB-TTL adapter:

```
adapter 5V ->5V     GND->GND     TX->U0R     RX->U0T
jumper IO0 -> GND, then press RESET to enter the bootloader
```

Remove the jumper and press RESET again to run. "Failed to connect" is
almost always IO0 not grounded at the moment the upload starts.

1. Arduino IDE → Boards Manager → **esp32** (Espressif). No extra
   libraries — `esp_camera`, `esp_http_server` and `img_converters` all
   ship with it.
2. `cp secrets.h.example secrets.h`, fill in WiFi and **a unique CAM_ID**.
3. Tools → Board → **AI Thinker ESP32-CAM**
4. Tools → Partition Scheme → **Huge APP (3MB No OTA)** — the default
   doesn't fit and fails *after* compiling, with a size error that reads
   like a code problem.
5. Serial Monitor at 115200 for the sensor, the address, and the exact
   line to paste into the Pi's config.

**Power is what actually bites.** These brown out on a weak 5V supply the
moment the radio transmits — a boot loop, or a stream that dies seconds
in, both looking like firmware bugs. Use a supply good for 500 mA+ and
short wires; many USB-TTL adapters can't deliver that from their 5V pin.

## Pointing the Pi at them

In `pi_app/.env`, comma-separated, `id=url`:

```
SAFETYFIRST_CCTV_URL=gate=http://safetyfirst-gate.local,yard=http://safetyfirst-yard.local
SAFETYFIRST_CCTV_INTERVAL=1.0
```

A bare URL works too — the id is derived from the hostname — but naming
them explicitly is worth the keystrokes.

**Leave the backend's own `CCTV_URL` blank.** Set, it makes the server
try to fetch a camera directly and time out, ignoring everything the Pi
relays. Blank is what selects relay mode.

Each camera gets its own thread on the Pi, so a dead one can't throttle a
healthy one behind it. Restart the gate to pick up config changes.

## Endpoints on each camera

| Path | What |
|---|---|
| `/` | one-page preview, shows the detected sensor — prove it works before involving the backend |
| `/stream` | multipart MJPEG, full rate |
| `/snapshot` | a single JPEG — what the relay polls |

The console polls `/snapshot` rather than consuming `/stream`. An `<img>`
tag can't send an `Authorization` header, so proxying a stream for a
long-lived `<img src>` would put the caller's JWT in a query string,
where it lands in logs and browser history. Polling costs frame rate and
keeps the token in a header. Watch `/stream` directly from the LAN when
you want full rate.

## When the picture is wrong

**Blank or garbled** — suspect the pin map before the lens. Camera pin
definitions differ per board; on the wrong one the sketch still compiles,
still joins WiFi, and still serves *something*. The map here is
AI-Thinker's.

**Upside down** — set `CAM_VFLIP` / `CAM_HMIRROR` in `secrets.h`. Do it
there, not in CSS: `/snapshot` has no CSS and every other viewer would
have to know.

**"no PSRAM"** — falls back to QVGA with one buffer. That's the honest
fallback: asking for VGA without PSRAM fails to allocate and the camera
never initialises at all.

**Console says offline but `/` works in a browser** — the *Pi* can't
reach the camera even though your laptop can. They're on different
networks; the relay resolves the URL from the Pi, not from you.

**One tile flickering between two scenes** — two boards share a
`CAM_ID`. Give them distinct ones and reflash.
