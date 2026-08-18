# Site CCTV camera (AI-Thinker ESP32-CAM)

A second camera watching the site, separate from the gate. The checkpoint
camera is busy deciding who gets in; pointing it at the yard would mean
choosing between the two.

**This is a monitoring view only — no PPE detection.** Frames are not run
through the model and raise no records. That was a deliberate choice: the
gate's inference is per-badge and bounded, while a continuously-analysed
second camera is a standing cost on the backend for footage nobody has
asked to be ruled on.

```
ESP32-CAM ──MJPEG/HTTP──► backend (cctv.py) ──HTTPS──► console
   (LAN, no auth)          proxies + authenticates      Site Camera page
```

## How it's wired up, and why

The camera holds no credentials and never calls the backend. The backend
reaches *it*. Three reasons:

- **Auth belongs in one place.** The camera can't tell an admin from a
  stranger; the console already can, so `/api/cctv/*` sits behind
  `@admin_required`.
- **Mixed content.** The console is served over HTTPS through a tunnel,
  and browsers refuse to load an `http://` image into an `https://` page.
  Proxying puts the picture on the same origin as everything else.
- **Containment.** Only the backend needs a route to the camera, so the
  camera stays on the LAN rather than being exposed.

The trade-off is that **the camera is unauthenticated on its own
network** — anyone on that WiFi can watch it by IP. The network is the
security boundary. Don't put it on a guest or public SSID.

## Flashing

This board has no USB port. You need a USB-TTL adapter:

```
adapter 5V ->5V     GND->GND     TX->U0R     RX->U0T
jumper IO0 -> GND, then press RESET to enter the bootloader
```

Remove the IO0 jumper and press RESET again to run. A "Failed to connect"
error is almost always IO0 not grounded when the upload starts.

1. Arduino IDE → Boards Manager → **esp32** (Espressif). No extra
   libraries — `esp_camera` and `esp_http_server` ship with it.
2. `cp secrets.h.example secrets.h` and fill in WiFi. Gitignored, same
   pattern as `ppe_sensors`.
3. Tools → Board → **AI Thinker ESP32-CAM**
4. Tools → Partition Scheme → **Huge APP (3MB No OTA)** — the default
   scheme doesn't fit, and the upload fails *after* compiling with a size
   error that reads like a code problem.
5. Serial Monitor at 115200 to see the address it came up on.

**Power is what actually bites.** This board browns out on a weak 5V
supply the moment the radio transmits — a boot loop, or a stream that
dies seconds in, both looking like firmware bugs. Use a supply good for
500mA+ and short wires; many USB-TTL adapters can't deliver that.

## Pointing the backend at it

The sketch advertises itself over mDNS as `safetyfirst-cam.local`, so the
address survives a DHCP lease change. Set in the backend's `.env`:

```
CCTV_URL=http://safetyfirst-cam.local
```

If mDNS doesn't resolve on your network (some routers and most corporate
WiFi block it), use the IP the Serial Monitor printed instead. Then
restart the backend — config is read at startup.

The **Site Camera** page appears in the admin sidebar. If `CCTV_URL` is
blank it says so plainly rather than showing a broken image.

## Endpoints on the camera

| Path | What |
|---|---|
| `/` | one-page preview, useful for proving it works before touching the backend |
| `/stream` | multipart MJPEG, the full-rate live feed |
| `/snapshot` | a single JPEG |

The console polls `/snapshot` rather than consuming `/stream`. An `<img>`
tag can't send an `Authorization` header, so proxying the stream for a
long-lived `<img src>` would mean putting the caller's JWT in a query
string, where it lands in logs and browser history. Polling costs frame
rate — a few per second instead of fifteen — and keeps the token in a
header. Watch `/stream` directly from a machine on the LAN when you want
the full rate.

## When the picture is wrong

**Blank or garbled frame** — suspect the pin map before the lens. Camera
pin definitions differ per board; on the wrong one the sketch still
compiles, still joins WiFi, and still serves *something*. The map in
`cctv_cam.ino` is AI-Thinker's.

**Upside down** — the OV2640 is commonly mounted inverted, so the sketch
sets `set_vflip` and `set_hmirror`. Flip them there, not in CSS: the
`/snapshot` endpoint has no CSS, and every other viewer would have to
know.

**"no PSRAM found" in Serial** — it falls back to QVGA (320×240) with a
single buffer. That's the honest fallback: asking for VGA without PSRAM
fails to allocate and the camera never initialises at all.

**Console says "Offline" but `/` works in a browser** — the backend can't
reach the camera even though your laptop can. They're on different
networks. `CCTV_URL` is resolved from the machine running `app.py`.
