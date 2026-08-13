# SafetyFirst — PPE Compliance Checkpoint

**Team Mojito** — original concept by **Jasbir Singh Monga**, built with
Bhavesh Waghmare and Priyal Vairagade.
Built for the FAR AWAY 2026 hackathon.

An access-control gate, not a monitoring dashboard. A worker presents an RFID
badge, a camera checks their PPE against the site's current policy, and the
gate either lets them through (recording attendance) or turns them away
(recording why, with a photo). Everything an admin can configure — what's
required, who's allowed, where the gate is — is a runtime setting, not a
redeploy.

This file is the map: what exists, how it fits together, how to run it, and
where it was left off. If you're picking this up cold (new machine, or
someone else on the team), read this section first, then the "Local setup"
section to get it running.

---

## Where things stand

**Working end-to-end**, verified against the real model and a real browser:

- Badge scan → worker lookup → live PPE check (YOLOv8: Hardhat, Safety Vest,
  Mask — see [Model limitations](#model-limitations)) → grant/deny → attendance
  record.
- A full admin console: overview, personnel (role filters, last-seen, CSV
  export), captures (evidence viewer), reports (CSV presets), analytics,
  checkpoint policy, site location, and an append-only change log.
- Configurable checkpoint policy (which PPE is required, confidence
  threshold) — takes effect on the next frame, no restart. All three
  surfaces (web dashboard, kiosk display, camera overlay) read the live
  policy instead of a hardcoded list.
- Evidence capture: refusals are photographed automatically (never grants —
  a deliberate privacy stance), snapshots are operator-triggered. Images
  auto-purge after 30 days; the decision record does not.
- An audit log for policy changes and personnel deletions — closes the
  accountability gap that making policy configurable would otherwise open.
- Live inference performance readout (FPS / latency) on the gate view.
- Spoken gate announcements ("Access granted.", "Put on your hardhat...") via
  ElevenLabs, cached on disk per phrase so a repeated sentence never pays for
  or waits on synthesis twice. Falls back to the browser's built-in voice
  with no configuration required — `ELEVENLABS_API_KEY` is optional.
- Site location: an admin-set point today, but the API and admin UI are
  already built to take a live fix the moment a GPS module is wired in (see
  [gps_reporter.py](pi_app/gps_reporter.py)) — no further backend changes
  needed when that hardware arrives.
- The Raspberry Pi checkpoint app (`pi_app/`) runs natively (not a browser),
  owns the camera and an MFRC522 RFID reader directly, and has a `doctor.py`
  pre-flight tool to diagnose a new Pi before trusting it at a real gate.
- A local attendance queue (`pi_app/offline_queue.py`) covering the one gap
  that mattered: a PPE check reaches a verdict, and then the network drops
  in the few seconds before that decision is recorded. That record is saved
  locally and retried automatically once the connection's back, instead of
  vanishing. This is not a full offline mode — badge lookup and the PPE
  check itself are both server-side, so if the connection is down before a
  verdict exists, there's nothing yet to lose.
- Sensor alerts: a critical alert (gas, etc.) holds the gate — it overrides
  PPE compliance entirely, since someone in full gear still isn't safe to
  admit into a hazard. It shows on the kiosk display, pauses the checkpoint
  app on the Pi even before anyone badges in, and pops up on every open
  admin/operator browser tab (polled, not pushed). An admin's **Alerts**
  page has the history and a "simulate" trigger for testing without real
  sensor hardware, which posts to the same endpoint the ESP32-main board
  will use once it exists — nothing here changes when it arrives.
- Threshold-based sensor readings: a device can report a raw value (e.g.
  gas ppm) instead of deciding severity itself — the backend classifies it
  against an admin-configured per-kind threshold (warning/critical level,
  unit, and whether higher or lower is worse) and raises the same alert a
  breach would. A kind with no threshold set just has its readings logged.
  Configured on the Alerts page, alongside a small live readout of the
  latest value per sensor. [`esp32-sim/`](esp32-sim/) can exercise this
  without any real sensor.

**ESP32-CAM + ESP32-main split** — one board dedicated to video streaming
(not yet wired), one to sensors and alert reporting. The sensor half is
built: [`esp32-main/ppe_sensors/`](esp32-main/ppe_sensors/) reads a real
MQ-9 gas sensor and DHT11 temperature/humidity sensor and posts to
`/api/gate/alerts` / `/api/gate/sensors`, the same endpoints the
[`esp32-sim/`](esp32-sim/) throwaway sketch used while this hardware
didn't exist yet — that sketch is kept as a hardware-free fallback.

**Designed, not yet built:**

**Untested on real hardware** — everything below has code but has never
touched actual hardware:

- The MFRC522 RFID reader (code is written including a same-badge-rescan
  fix; `pi_app/doctor.py` is ready to bring it up).
- Any GPS module (`pi_app/gps_reporter.py` supports NMEA-over-serial, e.g. a
  NEO-6M, but `SAFETYFIRST_GPS=off` is the default until one exists).
- The Waveshare UPS HAT (E) and its I²C fuel gauge.
- Local (on-device) inference via the AI HAT — see
  [Cloud vs. local inference](#cloud-vs-local-inference-deferred) below.

**Explicitly deferred, with reasoning kept here so it isn't re-litigated:**

### Cloud vs. local inference

Inference currently runs on the backend (cloud), not the Pi — keeps the Pi
install light (no torch/ultralytics on-device) and was cheap bandwidth-wise
when inference only fires while someone's actively being checked at a badge
scan. This gets revisited once the AI HAT is actually in hand and there's a
real benchmark to look at — a past project's experience with an AI HAT
underperforming on a similar pipeline is the reason for wanting a benchmark
before switching, not a bias against local inference. It also matters more
once multiple always-on cameras are added (continuous streaming, not just
per-badge checks) — that's a planned expansion, not yet configured.

### Why the model can't require gloves or boots

`best.pt` (YOLOv8) only has three classes: `Hardhat`, `Safety Vest`, `Mask`
(confirmed via `YOLO('best.pt').names`). The admin console's Checkpoint
Policy page only lets you require items the model can actually see —
requiring something it can never detect would refuse everyone, permanently.
Gloves/boots detection would need a retrained or additional model.

---

## Architecture

```
┌─ Raspberry Pi checkpoint (pi_app/) ─────┐
│  camera → checkpoint.py (fullscreen Tk) │
│  MFRC522 badge reader (SPI)             │
│  GPS reporter (optional, off by default)│
└───────────────┬──────────────────────────┘
                 │ HTTPS/JWT (device account)
                 ▼
┌─ Backend (backend/, Flask + SQLAlchemy) ─────────────────────┐
│  /api/auth      signup / login / guest                        │
│  /api/gate      badge lookup, attendance, GPS + alert report   │
│  /api/          detection session, /api/socket, /api/alerts    │
│  /api/admin     personnel, policy, evidence, reports, audit,   │
│                 alert history                                  │
│  ppe_detection.py → YOLOv8 (best.pt) inference                 │
│  site_settings.py → live, cached, admin-editable policy         │
│  SQLite locally / Postgres in production                       │
└───────────────┬──────────────────────────────────────────────┘
                 │ same REST API
                 ▼
┌─ Web frontend (frontend/, static HTML/JS, no build step) ─────┐
│  Public site (index.html) · Gate Control (browser-based,        │
│  visit-site.html) · full Admin Console (admin/violations/        │
│  reports/analytics/settings/gps/audit .html) · Device kiosk       │
│  UI (pi-home.html, kiosk.html) for the Pi's touchscreen           │
└────────────────────────────────────────────────────────────────┘
```

Three ways to hit the same backend: a browser (any operator/admin), the
Pi's own fullscreen native app (the actual gate), or the Pi's touchscreen
running the same web frontend in kiosk mode. All three read the same live
policy and write to the same database.

---

## Repo structure

```
backend/
  app.py              Flask app factory, CORS, JWT/DB setup, startup migrations
  config.py            Env-driven settings (DB URL, secrets, evidence dir, CORS)
  extensions.py         SQLAlchemy + JWTManager instances
  models.py              User, AttendanceRecord, DetectionRecord, SiteSetting, AuditEvent, SensorAlert, SensorReading
  auth.py                 /api/auth/* — signup, login, Google Sign-In, guest sessions
  gate.py                  /api/gate/* — badge lookup, attendance, GPS/alert/sensor-reading report (device-facing)
  detection.py              /api/start /stop /status /socket, /api/alerts/* — the live PPE-check loop
  admin.py                   /api/admin/* — personnel, settings, evidence, reports, audit, location, alerts
  site_settings.py             Live, cached, validated checkpoint policy (required PPE, confidence, location)
  alerts.py                     Cached "is a critical alert active" check + report/acknowledge
  evidence.py                    Refusal-frame capture, path-traversal-guarded serving, retention purge
  audit.py                        Append-only change log for policy/personnel/alert changes
  tts.py                            Spoken gate announcements (ElevenLabs), cached on disk per phrase
  ppe_detection.py                   YOLOv8 model loading + inference
  make_admin.py                    CLI: flag a user as admin
  seed_workers.py                   CLI: create demo workers with fake badge IDs
frontend/                            Static HTML/CSS/JS, no build step
  index.html                          Public marketing/status page
  login.html, signup.html               Auth
  visit-site.html                        Browser-based gate control (camera + live verdict)
  admin.html, alerts.html, violations.html, Admin console pages (all require Auth.requireAdmin())
  reports.html, analytics.html,
  settings.html, gps.html, audit.html
  pi-home.html, kiosk.html                Device-only kiosk UI (see js/shell.js's deviceOnly nav flag)
  history.html                             A worker's own attendance record
  js/                                       config.js (API base URL), auth.js (JWT storage/guards),
                                             shell.js (shared sidebar/nav), camera.js (getUserMedia)
  css/app.css                                Shared design system (all pages)
pi_app/                                      Native Raspberry Pi checkpoint app — see pi_app/README.md
  checkpoint.py                               Fullscreen Tk gate display, the actual entry point
  badge_reader.py                              MFRC522 (SPI) + keyboard fallback
  gps_reporter.py                              Serial NMEA GPS reader + no-op fallback
  offline_queue.py                              Local retry queue for attendance records lost to a network blip
  doctor.py                                    Pre-flight hardware/connectivity check
  ui.py                                        Tk drawing helpers
best.pt                                        Trained YOLOv8 weights (Hardhat/Safety Vest/Mask)
Dockerfile, render.yaml                        Backend container deploy (Render or similar)
vercel.json                                    Frontend static deploy (Vercel)
```

---

## Tech stack

- **Backend:** Python, Flask, SQLAlchemy, Flask-JWT-Extended, SQLite (dev) /
  Postgres (production-ready via `DATABASE_URL`).
- **ML:** YOLOv8 (Ultralytics), OpenCV.
- **Frontend:** Vanilla HTML/CSS/JS, no framework, no build step. Tailwind
  and Font Awesome via CDN. Shared design tokens in `css/app.css`.
- **Pi app:** Python + Tkinter (native fullscreen UI, not a browser) +
  OpenCV for camera capture only (inference stays on the backend).
- **Auth:** JWT, email/password or Google Sign-In, plus a guest mode.

---

## Local setup

### 1. Backend

```bash
python -m venv venv
venv\Scripts\activate        # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

copy .env.example .env       # macOS/Linux: cp .env.example .env
```

Edit `.env` — for local dev, `SECRET_KEY`/`JWT_SECRET_KEY` can be any random
string, and `GOOGLE_CLIENT_ID` can stay blank (email/password still works).

```bash
cd backend
python app.py
```

Listens on `http://localhost:5000`, creates `app.db` (SQLite) on first run.

Useful one-off scripts (run from `backend/`, with the venv active):

```bash
python make_admin.py you@example.com     # flag an account as admin
python seed_workers.py                    # create demo workers with fake badge IDs, for testing without a card reader
```

### 2. Frontend

No build step — just serve the static files (opening `index.html` as a
`file://` URL breaks camera access and Google Sign-In, which both require a
real origin):

```bash
cd frontend
python -m http.server 8000
```

Open `http://localhost:8000`. If the API isn't on `localhost:5000`, edit
`frontend/js/config.js`.

### 3. Raspberry Pi checkpoint app (optional)

Only needed to run the actual native gate app (as opposed to the
browser-based Gate Control page, which works fine for development without
any Pi). Full instructions, including the RFID reader and GPS module setup,
are in [`pi_app/README.md`](pi_app/README.md). Short version:

```bash
cd pi_app
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # set SAFETYFIRST_API to the backend's LAN IP, not localhost
python doctor.py        # pre-flight check before trusting the gate
python checkpoint.py
```

---

## Environment variables (backend `.env`)

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | `dev-secret-change-me` | Flask session secret — set a real random value in production |
| `JWT_SECRET_KEY` | `dev-jwt-secret-change-me` | Signs auth tokens — same caveat |
| `DATABASE_URL` | `sqlite:///app.db` | Set to a Postgres URL in production — SQLite on an ephemeral container filesystem is wiped on redeploy |
| `GOOGLE_CLIENT_ID` | *(blank)* | Optional — see Google Sign-In setup below |
| `CORS_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated frontend origins allowed to call the API |
| `EVIDENCE_DIR` | `backend/instance/evidence` | Where refusal photos are written — point at a mounted volume in production |
| `EVIDENCE_RETENTION_DAYS` | `30` | How long refusal images are kept before auto-purge |
| `ELEVENLABS_API_KEY` | *(blank)* | Optional — spoken gate announcements. Blank keeps the browser's own (robotic) voice; nothing breaks either way |
| `ELEVENLABS_VOICE_ID` | `21m00Tcm4TlvDq8ikWAM` ("Rachel") | Any voice_id from your ElevenLabs library |

Pi app env vars (`pi_app/.env`) are documented in
[`pi_app/README.md`](pi_app/README.md) and `pi_app/.env.example`.

### Setting up Google Sign-In (optional, 5 minutes)

1. [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → Create Credentials → OAuth client ID → Web application.
2. Add every origin you serve the frontend from under **Authorized JavaScript origins** (e.g. `http://localhost:8000`).
3. Put the Client ID in `backend/.env` (`GOOGLE_CLIENT_ID`) and `frontend/js/config.js` (`window.GOOGLE_CLIENT_ID`). Restart the backend.

Email/password auth works with none of this configured.

---

## Deployment

- **Backend:** any container host via the included `Dockerfile` (Render,
  Fly.io, Hugging Face Spaces...). `render.yaml` is set up for Render. Set
  `GOOGLE_CLIENT_ID`, `CORS_ORIGINS`, and `DATABASE_URL` as environment
  variables on the host — don't hardcode them.
- **Frontend:** static hosting (Vercel, Netlify...). `vercel.json` points
  Vercel at `frontend/`. Update `frontend/js/config.js` with the deployed
  backend URL first.

---

## Hardware status

Owned: Raspberry Pi 5 (8GB), AI HAT (Hailo, 12 TOPS — not yet used for
inference, see [above](#cloud-vs-local-inference-deferred)), Waveshare 10.1"
touch display, MFRC522 RFID reader, Quectel EC200U 4G module, GPS antenna
(not yet wired to a receiver module), Waveshare UPS HAT (E) — I²C fuel
gauge, 4×21700 cells, ~4hr backup, 5V/6A out.

Owned and wired: an ESP32-C3 SuperMini running
[`esp32-main/ppe_sensors/`](esp32-main/ppe_sensors/), with a real MQ-9 gas
sensor and DHT11 temperature/humidity sensor attached — deliberately kept
off the Pi's own GPIO so sensor timing doesn't compete with camera/badge
work on the same board. Planned, not owned yet: a dedicated ESP32-CAM for
video streaming, as a second board rather than adding a camera to the C3.

`pi_app/doctor.py` is the tool to run the moment the RFID reader or GPS
module get wired in — neither has touched real hardware yet.

---

## Model limitations

`best.pt` detects exactly three classes: `Hardhat`, `Safety Vest`, `Mask`.
It cannot detect gloves or boots — the admin console's policy editor only
offers items the model can see, on purpose (see
[above](#why-the-model-cant-require-gloves-or-boots)).

---

## Other docs in this repo

- [`pi_app/README.md`](pi_app/README.md) — full Pi setup, wiring, systemd
  service, troubleshooting.
- [`ROUND2_SETUP.md`](ROUND2_SETUP.md) — a historical setup checklist from
  an earlier round of this project (pre-dates the admin console, gate
  hardware, and everything under "Where things stand" above). Kept for
  history; this README supersedes it.

## Credits

- **Jasbir Singh Monga** — original concept and project owner.
- Bhavesh Waghmare, Priyal Vairagade — Team Mojito, build.
- YOLOv8 by Ultralytics (model trained by the team).
- Tailwind CSS + Font Awesome for styling (via CDN, no build step).
