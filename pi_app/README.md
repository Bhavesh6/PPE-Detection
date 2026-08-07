# SafetyFirst — Raspberry Pi Checkpoint

A fullscreen gate display that owns the camera directly and shows the
grant/deny ruling for whoever steps in front of it.

Running natively rather than in a browser removes every constraint a web page
carries at a gate: no HTTPS requirement for camera access, no cache, no kiosk
flags, no permission prompt — and a direct path to GPIO for the badge reader
(implemented — see below) and a lock relay (not built yet).

```
┌── Raspberry Pi ────────────┐        ┌── Backend ──────────────┐
│  camera → checkpoint.py    │ ─────► │  /api/socket → YOLOv8   │
│  fullscreen verdict display│ ◄───── │  verdict + detections   │
└────────────────────────────┘        │  record → database      │
                                      └──────────┬──────────────┘
                                                 │
                                      web admin & worker records
```

Inference runs on the **backend**, not the Pi, so this install stays light —
no torch, no ultralytics.

## Install (on the Pi)

```bash
sudo apt update
sudo apt install -y python3-tk python3-venv
```

`python3-tk` is required — it is not bundled with Raspberry Pi OS's Python.

```bash
cd pi_app
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Badge reader (MFRC522, optional but recommended)

Without it the app falls back to typing tags on a keyboard — fine for
development, not for a real gate.

```bash
sudo raspi-config   # Interface Options -> SPI -> Yes, then reboot
pip install spidev RPi.GPIO mfrc522
```

Wiring (BCM): `SDA→GPIO8(CE0)  SCK→GPIO11  MOSI→GPIO10  MISO→GPIO9  RST→GPIO25  3.3V→3.3V  GND→GND`
(3.3V, not 5V — the module doesn't tolerate 5V).

### GPS module (optional, not required to run the gate)

Off by default — the checkpoint's location is whatever an admin sets by
hand in the console's **Site Location** page. If an NMEA GPS module (e.g. a
NEO-6M) is wired up over serial:

```bash
pip install pyserial pynmea2
```

then set in `.env`: `SAFETYFIRST_GPS=auto` and `SAFETYFIRST_GPS_PORT` to the
module's serial port (default `/dev/ttyUSB0`). See `gps_reporter.py`.

## Configure

```bash
cp .env.example .env
nano .env
```

Set `SAFETYFIRST_API` to a URL the Pi can actually reach — the backend
machine's LAN IP (e.g. `http://192.168.1.9:5000`), not `localhost`, unless the
backend runs on the Pi itself.

Create a device account through the web sign-up and put those credentials in
`.env`. Without them the app opens a guest session, which works but files
every decision against a throwaway account.

## Pre-flight check

Before trusting the gate — especially the first time on a new Pi — run:

```bash
python doctor.py            # everything except an actual card/GPS read
python doctor.py --scan     # also waits for a real badge scan and a GPS fix
```

It walks the chain from kernel to badge to backend (platform, SPI, reader
libraries, camera, backend reachability, device credentials, site policy,
GPS) and says which link is broken instead of leaving you to guess from a
blank screen at demo time.

## Run

```bash
source venv/bin/activate
python checkpoint.py
```

Press **Esc** or **q** to exit.

While developing on a laptop, `SAFETYFIRST_WINDOWED=1` runs it in a normal
window instead of taking over the screen.

## Start automatically on boot

Create `/etc/systemd/system/safetyfirst.service`:

```ini
[Unit]
Description=SafetyFirst Checkpoint
After=graphical.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/PPE-Detection/pi_app
Environment=DISPLAY=:0
ExecStart=/home/pi/PPE-Detection/pi_app/venv/bin/python checkpoint.py
Restart=always
RestartSec=5

[Install]
WantedBy=graphical.target
```

Adjust `User` and the paths if you are not on the default `pi` account, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now safetyfirst
sudo journalctl -u safetyfirst -f     # watch the logs
```

`Restart=always` brings the checkpoint back up if it crashes or if the
backend was unreachable at boot.

## Troubleshooting

**"Camera 0 not available"** — list what the Pi can see with
`v4l2-ctl --list-devices` (`sudo apt install v4l-utils`), then set
`SAFETYFIRST_CAMERA` to the right index.

**"Cannot reach the API"** — from the Pi, check
`curl http://<backend-ip>:5000/api/health`. If that fails it is the network or
a firewall, not this app. The backend must bind `0.0.0.0`, which `app.py`
already does.

**"Device credentials rejected"** — the email/password in `.env` do not match
an account. This deliberately does *not* fall back to guest, so the gate never
silently detaches from its named device account.

**Verdict never leaves "Step Up"** — the model has to see a `Person` before it
rules on anything. Check the detection log in the web admin console to confirm
frames are arriving.

## Notes

- The display runs at camera rate while inference is throttled to
  `SAFETYFIRST_INTERVAL` (0.5s default) — a person does not change PPE thirty
  times a second, and sending every frame would only load the API.
- Required PPE is set live from the admin console's **Checkpoint Policy**
  page (`backend/site_settings.py`), not hardcoded — the gate and the web
  dashboard read the same policy, so changing it takes effect on the next
  frame with no redeploy.
- Local (on-Pi) inference with the AI HAT is a future option; it needs the
  model converted `.pt → ONNX → HEF` with Hailo's compiler.
