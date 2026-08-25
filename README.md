---
title: SafetyFirst PPE Detection
emoji: 🦺
colorFrom: blue
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

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

## For judges

**Live site:** [resistance-exclusion-divorce-expo.trycloudflare.com](https://resistance-exclusion-divorce-expo.trycloudflare.com)

**Demo logins** — two accounts, created specifically for judging so no
real person's password is anywhere in this repo. Both work through the
link above right now.

| Role | Email | Password |
|---|---|---|
| Admin (full console) | `judge-admin@safetyfirst.demo` | `JudgeDemo2026!` |
| Operator (a worker's own view) | `judge-worker@safetyfirst.demo` | `JudgeDemo2026!` |

No login at all is needed to see the guest experience — "Try it" on the
homepage, or [Gate Control](https://resistance-exclusion-divorce-expo.trycloudflare.com/visit-site.html)
directly. What each role can actually see is laid out in
[Roles & access](#roles--access).

One honest caveat about the link itself: it's a Cloudflare *quick* tunnel
off a laptop, not a permanent host — if it's down when you read this, the
project is otherwise unaffected, and [DEPLOY.md](DEPLOY.md) has the
sequence for a permanent Hugging Face Space + Vercel deployment, which is
built and tested but not yet where this link points.

**Demo video:** [photos.app.goo.gl/Hfjp38x16mtywTMg9](https://photos.app.goo.gl/Hfjp38x16mtywTMg9)

The video walks through the hardware — the gate master, the two sensor
nodes, the two ESP32-CAMs, the GPS module, the Pi checkpoint display — and
shows a live badge scan and PPE check. If the recording cuts before every
piece is shown, that is a filming constraint, not a hardware one: everything
named below as "tested" was tested with the physical part attached, over
USB or ESP-NOW, on the actual Raspberry Pi 5 this project runs on, and the
results (satellite counts, sensor readings, detection records with real
timestamps) are quoted where we have them rather than asserted.

**This is a prototype, and we are saying exactly where that shows.** The
software stack — the backend, the console, the admin tooling, the offline
behaviour — is built to the standard we'd ship, and we mean that: it has
input validation, audit logging, retry queues, rate limiting, and it was
attacked by its own authors looking for the ways it would break (see
[Response to judge feedback](#response-to-judge-feedback) below). The
hardware is real and mostly working, but it is five ESP32 boards and a Pi
wired on a bench, not a manufactured product — expect a reflash to
occasionally drop mid-write and need a retry, expect a GPS fix to need a
clear view of sky, expect a USB device to need reseating after a reboot.
Where something is aspirational rather than built, it's labelled that way
in [Where things stand](#where-things-stand) rather than folded in with
what's proven.

**What's genuinely running end-to-end**, not simulated for the video:
badge scan → worker lookup → live camera PPE check → grant/deny →
attendance record, with two sensor nodes reporting gas/temperature/humidity
over ESP-NOW through the gate master, a GPS module reporting the
checkpoint's position, and — the part we're proudest of — the whole chain
still working with the backend switched off entirely, ruling on-device and
syncing the moment the network returns. That's backed by a UPS HAT on the
Pi itself, rated for hours of runtime, because on a real site a power cut
and a network outage tend to be the same event — see
[Running with no internet](#running-with-no-internet) for the software
side of that story and [Hardware status](#hardware-status) for the power
side.

### Against the judging parameters

Software and hardware are both being scored on task implementation,
complexity, execution, innovation, functionality/reliability, and
documentation — plus PCB/build quality specifically for hardware. Rather
than make a judge dig, here's where the evidence for each one actually
lives:

| Parameter | Where to look |
|---|---|
| Task implementation | [Response to judge feedback](#response-to-judge-feedback) is the direct answer to this round's brief; [Where things stand](#where-things-stand) is the full feature list |
| Task complexity | Five ESP32 boards on three different protocols (ESP-NOW, USB serial, HTTPS/JWT) feeding one system — [How the hardware talks to the software](#how-the-hardware-talks-to-the-software); dual-path inference with automatic handover — [Running with no internet](#running-with-no-internet) |
| Technical execution | [Response to judge feedback](#response-to-judge-feedback) names five specific defects found and fixed, with the failure mode each one caused, not just "it works now" |
| Innovation & creativity | Capability-link notices that need no account to answer (not a generic "share link"), a *computed* status that structurally cannot drift from what happened, an inference fallback that hands control back automatically and logs the handover both ways |
| Functionality & reliability | The concurrent-issue retry fix, the per-caller rate-limit fix, the single-instance lock that stops two gates fighting over one serial port, `doctor.py` reading the same config the gate actually runs on — all in [Response to judge feedback](#response-to-judge-feedback) and [Hardware status](#hardware-status) |
| Documentation & presentation | This file, [DEPLOY.md](DEPLOY.md), [pi_app/README.md](pi_app/README.md), and the demo video above |
| **Software:** architecture, code quality, UX, scalability | [Architecture](#architecture), [Repo structure](#repo-structure) (one file per responsibility, not a monolith), [Roles & access](#roles--access) for the permission model, [Deployment](#deployment) for the explicit single-worker-process reasoning (what scales today, what has to move to Redis/Postgres first to add a second worker) |
| **Hardware:** PCB, board quality, circuit integration, component selection | See directly below — this is the one dimension we did not build to an industrial standard, and we'd rather say so than have it read as an oversight |

**On PCB and build quality specifically:** this is off-the-shelf ESP32
dev boards and breakout modules on point-to-point wiring, not a custom
carrier board — there is no PCB design in this project, and pretending
otherwise would be worse than just saying it. What we did put engineering
into at the hardware layer was the *system* design given that constraint:
one board per responsibility rather than one board doing everything (so a
sensor fault can't stall the badge reader), a protocol chosen per link
based on what it's actually carrying (ESP-NOW for battery nodes with no
Wi-Fi association overhead, one shared USB serial line for the master so
badges/hazards/fan-control can't end up split across two unsynchronised
paths), and device identification by USB vendor ID + handshake rather than
positional guessing, specifically because bench wiring is exactly where
"the first `/dev/ttyUSB*`" silently breaks. A next revision's honest
to-do list: a carrier PCB for the gate master (RC522 + fan driver + USB,
which are currently three separate modules and a breadboard), and a
connectorized harness in place of jumper wires, which is also almost
certainly why two of our reflashes this round dropped mid-write.

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
  policy instead of a hardcoded list. The browser-based Gate Control page
  draws live bounding boxes over the camera feed as the model sees them —
  what's detected is visible, not a verdict handed down from a black box.
- Evidence capture: refusals are photographed automatically (never grants —
  a deliberate privacy stance), snapshots are operator-triggered. Images
  auto-purge after 30 days; the decision record does not.
- An audit log for policy changes and personnel deletions — closes the
  accountability gap that making policy configurable would otherwise open.
- Live inference performance readout (FPS / latency) on the gate view.
- **Safety notices**: the way a refusal leaves this system and reaches
  somebody who has no login — a subcontractor's supervisor, an agency, a
  site manager. A notice is issued against a worker with the refusals it
  concerns, gets a reference and a due date, and travels as a link that
  needs no account, opens only that one notice, and expires. The recipient
  reads it with the evidence and answers it: accepted with a note on what
  they will do, or disputed with a reason. Status is computed from the
  record rather than stored, so it cannot drift from what happened. Each
  notice exports as JSON and the list as CSV, so another system can consume
  it without screen-scraping. See [backend/notices.py](backend/notices.py).
- **Inference survives the network.** Detection runs on the backend, and the
  gate falls back to an on-device ONNX model whenever the backend cannot be
  reached, then hands back when it returns — logging each handover.
  Verified with the backend switched off entirely: the gate resolved a badge
  from its local cache, ruled on-device, queued three decisions, and synced
  all three the moment the backend came back. A site is where the connection
  is worst and the gate matters most.
- **USB devices are identified, not counted.** The gate master ESP32 and the
  GNSS modem are matched on USB vendor id, asked to identify themselves, and
  remembered by serial number so replugging into another socket still finds
  them. The modem presents seven serial interfaces; taking "the first
  /dev/ttyUSB*" hands the gate one of those instead of the master and badge
  scanning stops the moment location is plugged in. See
  [pi_app/usb_devices.py](pi_app/usb_devices.py).
- **The gate opens like an appliance.** A hard-hat icon on the Pi's desktop,
  no terminal: `pi_app/install_launcher.sh`. The launcher discovers the
  display itself, holds a lock so a second tap cannot start a rival gate
  fighting over the camera and serial port, and puts failures on screen
  because there is no console behind an icon.
- **The public homepage is deliberately minimal** — four sections, one
  authored motion, no invented metrics — because the first version tried
  to summarise the whole project on one page and read as generic rather
  than specific. The original stays reachable as "The full story"
  (`index-full.html`) rather than being deleted, and both versions carry
  the same sign-in awareness: a returning admin sees "Dashboard" instead
  of "Sign in", not the same link regardless of who's looking — a real
  regression the rewrite introduced and this round fixed.
- Spoken gate announcements ("Access granted.", "Put on your hardhat...") via
  ElevenLabs, cached on disk per phrase so a repeated sentence never pays for
  or waits on synthesis twice. Falls back to the browser's built-in voice
  with no configuration required — `ELEVENLABS_API_KEY` is optional.
- **Site location is live**, not admin-set: a Quectel EC200U GNSS modem
  reports the checkpoint's actual position (see
  [Hardware status](#hardware-status) for the tested caveat around
  satellite view), and the console badge reads "Live from device" the
  moment a real fix lands rather than the manual pin an admin typed in.
  Falls back to that manual pin with no module attached. See
  [gps_reporter.py](pi_app/gps_reporter.py).
- The Raspberry Pi checkpoint app (`pi_app/`) runs natively (not a browser),
  owns the camera and the badge reader directly (via the gate master's
  RC522, over USB — see
  [How the hardware talks to the software](#how-the-hardware-talks-to-the-software)),
  and has a `doctor.py` pre-flight tool that reads the gate's own `.env`
  and diagnoses a new Pi before trusting it at a real gate.
- **Badge lookup and the PPE check both work with no backend connection
  at all**, not just the attendance write — see
  [Running with no internet](#running-with-no-internet) for the local
  worker cache, the on-device inference fallback, and the offline queue
  (`pi_app/offline_queue.py`) that catches a verdict reached with the
  network down, all three verified together in one test.
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

**A board per job, not one board doing everything** — five ESP32s, each
with a single responsibility, all reaching the Pi or something that does.
Two sensor nodes ([`ppe_sensors/`](esp32-main/ppe_sensors/)) read real
MQ-9 and DHT11 hardware and post to `/api/gate/alerts` and
`/api/gate/sensors`; two cameras ([`cctv_cam/`](esp32-main/cctv_cam/))
serve MJPEG on the LAN and are relayed to the console by the Pi; the gate
master ([`gate_master/`](esp32-main/gate_master/)) carries badges, hazard
packets and the cooling fan down one USB line.

Each sensor node is named with a `NODE_ID` that prefixes its readings
(`yard_gas` rather than `gas`), because the backend keeps one live row per
kind — two unnamed nodes would overwrite each other and the console would
show whichever spoke last. Naming them also buys per-node thresholds,
which is what you want anyway: a gas limit that suits the mixer is not the
one for an open yard.

[`esp32-sim/`](esp32-sim/) is kept as a hardware-free fallback — it signs
in identically and reports typed values, so the alert pipeline can be
demonstrated if a node's wiring fails at the venue.

**Tested on real hardware this round** — badge scanning (via the gate
master's RC522, including with the backend offline), the GNSS module
(fix chain confirmed end-to-end; needs sky view for a lock — see
[Hardware status](#hardware-status)), the USB camera (including
unplug/replug recovery across a Pi reboot), both sensor nodes over
ESP-NOW, and on-device CPU inference as a live fallback when the backend
is unreachable. Full detail, including the honest caveats, is in
[Hardware status](#hardware-status) — this bullet list is a summary, not
the source of truth.

**Still not exercised on this hardware:**

- A live, timed power-cut endurance test of the Waveshare UPS HAT (E) —
  it's installed and wired specifically to keep the gate alive through a
  mains outage (see [Hardware status](#hardware-status)), but the ~4–5hr
  figure is the component's rated capacity, not something we've personally
  timed on this bench yet.
- Local (on-device) inference *accelerated by the AI HAT* — the fallback
  above uses plain CPU ONNX, not the Hailo NPU, which is a different and
  smaller claim. See
  [Cloud vs. local inference](#cloud-vs-local-inference) below.

**Explicitly deferred, with reasoning kept here so it isn't re-litigated:**

### Cloud vs. local inference

Detection now runs in **both** places, deliberately, not one or the other.
The backend is primary — inference there keeps the Pi's normal install
light (no torch/ultralytics needed on-device for the everyday path) and
is cheap bandwidth-wise since it only fires while someone's actively being
checked at a badge scan. But a construction site is exactly where the
network is worst, so the Pi *also* carries a copy of the same trained
model, run through `onnxruntime` on plain CPU, and switches to it the
moment the backend stops answering — see
[Running with no internet](#running-with-no-internet) for how that's
wired and how it was verified. `SAFETYFIRST_INFERENCE=backend` or
`=local` pin it to one side only, if a deployment ever wants that.

What hasn't happened yet is running that on-device model *through the
Hailo AI HAT* rather than the Pi's own CPU — the accelerator is attached
and unused for inference. That gets revisited once there's a real
benchmark to look at: a past project's experience with an AI HAT
underperforming on a similar pipeline is the reason for wanting a
benchmark before switching accelerators, not a bias against local
inference — the CPU fallback above proves local inference itself works
fine on this hardware; the open question is purely whether the Hailo NPU
is worth the added complexity over it. It also matters more once multiple
always-on cameras are added (continuous streaming, not just per-badge
checks) — that's a planned expansion, not yet configured.

### Why the model can't require gloves or boots

`best.pt` (YOLOv8) only has three classes: `Hardhat`, `Safety Vest`, `Mask`
(confirmed via `YOLO('best.pt').names`). The admin console's Checkpoint
Policy page only lets you require items the model can actually see —
requiring something it can never detect would refuse everyone, permanently.
Gloves/boots detection would need a retrained or additional model.

---

## Response to judge feedback

The judging brief for this round was: *"Improve the part of your existing
MVP most related to feedback so that it can exchange information cleanly
with another format, workflow, or stakeholder. The work should include
both user-facing behaviour and the product state needed to support it."*

**Reading the brief.** The MVP's existing "feedback" is a refusal at the
gate — a worker turned away for missing PPE. Before this round, that
feedback lived entirely inside the system: an admin could see it in
Captures, but there was no route for it to reach anyone who isn't logged
in — a subcontractor's supervisor, an agency, a site manager who needs to
know one of their people was turned away and what they intend to do about
it. That is the "another stakeholder" the brief is asking about, and it is
a real gap: construction sites run on subcontractor relationships, and the
person accountable for a worker's compliance is very often not the person
standing at the gate.

**What we built: Safety Notices.** A formal, trackable path from a refusal
to the person responsible for it, without giving that person a login.
Issue a notice against a worker, attaching the refusals it concerns — it
gets a reference (`SN-2026-0041`) and a due date. The recipient gets a
link: no account, opens only that one notice, expires on its own. They see
the evidence and answer it — accept, with a note on what they'll do about
it, or dispute, with a reason (a dispute has to say why; that's the whole
point of the button existing). Status — issued, opened, overdue,
acknowledged, disputed, withdrawn — is *computed* from the record rather
than stored as a column, on purpose: a stored status can drift from what
actually happened the moment someone forgets to update it on a write path;
a computed one cannot lie, because it's derived fresh from `revoked_at`,
`acknowledged_at`, `due_at`, `delivered_at` every time it's read. Every
notice exports as JSON and the full list as CSV, so a site's own systems
can consume it without scraping a page meant for a person. Code:
[`backend/notices.py`](backend/notices.py),
[`frontend/notices.html`](frontend/notices.html) (issue/track),
[`frontend/notice.html`](frontend/notice.html) (the no-login recipient
page).

**How we decided this was the right feature**, not just a feature: we
spent a session doing nothing but attacking our own design before writing
code, and again after — the same question asked from every angle it would
break: what happens if the network drops mid-send, if two admins issue a
notice on the same worker at once, if a recipient's link gets forwarded,
if the due date and the evidence-retention window disagree about how long
a notice should live. That process surfaced real defects, and they were
fixed before this was called done, not filed as follow-ups:

- **A stored `status` column that could lie.** An early version wrote a
  status string when a notice was issued or answered. A crash or a race
  between two requests could leave that string wrong forever, and nothing
  would ever notice. Rebuilt as a computed property instead — see
  [`backend/models.py`](backend/models.py).
- **Concurrent issue caused a 500.** Issuing several notices in the same
  instant hit a reference-number collision — 3 of 5 requests crashed in a
  stress test. Fixed with a retry loop around the `IntegrityError`; 5 of 5
  succeed now.
- **Rate limiting wasn't per-caller behind a proxy.** The notice pages
  need no login, so the rate limiter is their only defence — and with
  `TRUSTED_PROXY_HOPS` unset, 24 distinct callers behind one proxy shared
  a single bucket and started locking each other out at the 21st. This is
  also why `render.yaml` now pins `TRUSTED_PROXY_HOPS=1` for the hosted
  deployment (see [Deployment](#deployment)).
- **A link could outlive the evidence it points to.** The notice's link
  expiry and the evidence photo's retention window were computed from
  different starting events; a comment in an early draft claimed they were
  "matched" and they weren't — the link was granting access to a page
  whose photo could already be gone. Fixed by deriving the link's expiry
  from the evidence window directly, so it can never expire later than the
  thing it links to.
- **An acknowledgement could be silently dropped.** The audit-log call on
  the no-login acknowledge route tried to read a JWT identity — but that
  route has no JWT, by design; it raised, and the write was lost with no
  error surfaced anywhere. Wrapped so an unauthenticated actor is logged
  correctly instead of crashing the write it was supposed to record.

**The user-facing half and the "product state" half, both required by the
brief:** the console pages (issue, track, filter, resend, withdraw) are the
user-facing behaviour; the schema, the retry-safe issuance, the
capability-link auth model, and the rate-limit fix are the product state
that behaviour depends on. Neither was treated as sufficient on its own —
a page that lets you issue a notice a concurrent request can crash is not
"working" by our own standard, even though it looks fine on the first try.

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

## Roles & access

There are three human roles, plus one non-human account type. Access is
enforced server-side (`@admin_required`, `@jwt_required`) on every route,
not just hidden in the UI — a worker calling an admin endpoint directly
gets a 403, not a page that happens not to show a button.

| | Guest | Operator (worker) | Admin |
|---|---|---|---|
| Try the live PPE check (Gate Control) | ✅ | ✅ | ✅ |
| Persistent identity, own attendance history | ❌ (session only) | ✅ | ✅ |
| Safety notices addressed to them, on their record | ❌ | ✅ | ✅ |
| Personnel — accounts, badges, roles | ❌ | ❌ | ✅ |
| Checkpoint Policy — required PPE, confidence threshold | ❌ | ❌ | ✅ |
| Alerts — thresholds, live readings, history, simulate | ❌ | ❌ | ✅ |
| Safety Notices — issue, track, withdraw | ❌ | ❌ | ✅ |
| Captures — refusal evidence | ❌ | ❌ | ✅ |
| Analytics, Reports (CSV export) | ❌ | ❌ | ✅ |
| Change Log (audit trail) | ❌ | ❌ | ✅ |
| Site Cameras, Site Location | ❌ | ❌ | ✅ |
| Acknowledge a critical sensor alert | ❌ | ✅ | ✅ |

Acknowledging a hazard alert is deliberately **not** admin-only — it's a
safety action, not a policy edit, and a gas alarm shouldn't wait on an
admin being reachable. Who cleared it is still recorded in the audit log
either way.

**Guest** — no signup, no persistent identity. Lands here from "Try it" on
the homepage; gets a session good for the live demo and nothing else.
Every guest-facing page nudges toward signing up, because a guest session
that vanishes on tab-close is the whole reason the history page and safety
notices exist for a real account.

**Operator** — a signed-up worker or contractor account. Sees their own
gate history (granted/denied, what was missing on a denial) and any safety
notice issued about a refusal of theirs — but the *reply* to a notice
belongs to the person it's addressed to, not to the worker it's about, so
an operator can read one but not answer it.

**Admin** — full console. The one thing worth calling out: admin is
granted by `ADMIN_EMAILS` at deploy time or by `make_admin.py` locally,
never by self-service signup — see [Deployment](#deployment) for why that
matters specifically on a hosted instance.

**Device account** — the Pi checkpoint and the ESP32 boards behind it
don't browse the console; they sign in as a dedicated account
(`device@safetyfirst.local` in this deployment) that's flagged non-guest
so it can post attendance, sensor readings and alerts, but carries no
admin rights. Every reading, alert and attendance record in the database
is attributable to a specific signed-in identity — device or human — never
anonymous.

### The in-app help assistant

Every console page carries a chat widget (bottom corner) — click it and
ask a question in plain language rather than hunting through menus. It's
scoped to what the asking account can actually see: an operator asking
about sensor thresholds gets told that's admin-only, not walked through a
page they can't open.

With a `GEMINI_API_KEY` or `GROQ_API_KEY` configured, it's a real
conversation — it can be asked "why is this refusal disputed" or "how do
I lower the false-refusal rate" and reason about the actual page context
it's given. Without a key configured, it doesn't go dark: it answers from
a built-in, role-scoped guide covering every admin section (cameras,
alerts, policy, notices, reports, analytics, personnel, the audit log,
location, captures) and a worker's own records, and says plainly that it's
the built-in guide rather than pretending to be the full assistant. That
matters for a demo specifically — a chatbot that goes silent the moment a
free-tier quota runs out is a worse look than one that degrades. See
[`backend/chatbot.py`](backend/chatbot.py).

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
  notices.py                        Safety notices — issue, capability links, replies, exports
  chatbot.py                         In-app help assistant, with a built-in guide when no key is set
  make_admin.py                    CLI: flag a user as admin
  seed_workers.py                   CLI: create demo workers with fake badge IDs
frontend/                            Static HTML/CSS/JS, no build step
  index.html                          Public homepage — deliberately minimal (see below)
  index-full.html                       The original, longer homepage — kept, linked as "The full story"
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

The `Dockerfile` builds **one container serving both the API and the
console**, so there is a single public URL and the console needs no API
address configured — it uses its own origin. Works on Hugging Face
Spaces (the YAML frontmatter at the top of this file is already set up
for it), Render, Fly.io, or anything that runs a container.

A split deployment works too — backend on a Space, frontend on Vercel.
`vercel.json` and `scripts/vercel-build.js` write the API's URL into the
frontend at build time from an `API_BASE_URL` project variable, so the
backend's address lives in a host setting rather than in the source.
**[DEPLOY.md](DEPLOY.md) has the full sequence**, which matters: the two
hosts each need the other's URL, so neither can be finished in one pass.

**Two variables are required.** The app *refuses to start* on a known
host if they're missing, rather than serving traffic with signing keys
that are published in this repository:

```
SECRET_KEY          python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET_KEY      (a second, different one)
```

**Set `ADMIN_EMAILS` before anyone signs up.** Admin rights are otherwise
granted only by `make_admin.py`, which needs a shell inside the
container — and a managed host does not give you one. Without it the
first person to sign up gets an ordinary account and *nothing on the
running system can promote it*: the console, policy, personnel, reports
and notices are all unreachable. Listing an email here makes that account
an administrator from its first request, and it is re-applied at every
start, so setting the variable and creating the account can happen in
either order.

**Set `TRUSTED_PROXY_HOPS=1` on any managed host.** They terminate TLS at
their own load balancer, so every request arrives from a single address.
Left at the default `0` the rate limiter counts the whole internet as one
caller and real users lock each other out — the public notice pages
first, where the limiter is the only protection there is.

**Set `DATABASE_URL` too, in practice.** Spaces and Render have
ephemeral filesystems: the default SQLite file and every evidence photo
are wiped on each restart or redeploy, taking the compliance record with
them. Point it at managed Postgres and the decisions survive. Evidence
images still need a mounted volume (`EVIDENCE_DIR`) — without one, the
records outlive the photos they reference.

Optional: `GOOGLE_CLIENT_ID`, `GEMINI_API_KEY` / `GROQ_API_KEY` (help
chatbot), `ELEVENLABS_API_KEY` (spoken announcements), `CCTV_UPLOAD_TOKEN`
(lets a camera post frames when the gate device is off). Each is blank by
default and its feature degrades quietly rather than breaking.

`CORS_ORIGINS` is **not** needed for the single-container deployment —
same origin, so no cross-origin request happens. It only matters if you
host the console separately.

**Do not raise the gunicorn worker count.** The image pins `-w 1` and
uses threads instead. The process holds the latest frame from each CCTV
camera and each user's live detection state in memory; a second worker
gets its own copy, so frames posted to one become invisible to a viewer
served by the other. It fails intermittently and per-request. Moving that
state to Redis or the database is the prerequisite for scaling out.

**Split hosting still works** if you want the console on a CDN: deploy
`frontend/` anywhere static and either set `PRODUCTION_API` in
`frontend/js/config.js` or load it once with `?api=https://your-backend`
(remembered afterwards). Then `CORS_ORIGINS` must list that origin.

**What hosting does not solve:** the Pi and the ESP32 nodes reach the
backend *outward*, so they need its public URL in their config — but
nothing reaches *inward* to them. A cloud-hosted backend still cannot
fetch from a camera on the site's LAN, which is why the Pi relays camera
frames rather than the server pulling them.

---

## How the hardware talks to the software

Three protocols, each chosen for what it's carrying, not one generic
"send everything over Wi-Fi" pipe:

**ESP-NOW, sensor nodes → gate master.** The two sensor boards
([`ppe_sensors/`](esp32-main/ppe_sensors/)) are battery/mains devices out
in the yard, not near a router, so they talk ESP-NOW — a
connectionless, low-power broadcast protocol built into the ESP32 radio,
no Wi-Fi association needed. Each node reads its MQ-9 gas sensor and
DHT11 temperature/humidity sensor, prefixes the reading with its
`NODE_ID` (`yard_gas`, not `gas` — two unnamed nodes would overwrite each
other's readings in the database) and sends it. The master's receive
callback logs every send/fail so a node going out of range shows up as a
fact, not a silent gap in the data.

**USB serial, gate master ↔ Raspberry Pi.** One wire carries everything
the master knows: `BADGE <tag>` when a card is scanned, `READING <kind>
<value> <unit>` relayed straight from the ESP-NOW mesh, `ALERT <kind>
<severity> <message>` for a hazard, and `FAN <duty> <rpm>` telemetry —
all line-based, one message per line, unknown lines silently ignored so
the Pi's own comment lines never confuse the parser. The Pi answers back
down the same wire with `TEMP <celsius>` so the master's fan curve tracks
the Pi's actual temperature, not a fixed guess. Because it's one wire
carrying badges *and* hazards *and* fan control, only one process on the
Pi may ever hold it open — see
[USB devices are identified, not counted](#where-things-stand), which
exists specifically so a second serial device (the GPS modem) can never
be mistaken for this one.

**HTTPS + JWT, Pi → backend.** The Pi signs in as a real device account
(never a guest — see [Roles & access](#roles--access)) and everything
after that is a normal authenticated REST call: badge lookup, attendance
recording, sensor-reading and alert reporting, camera-frame POSTs for
detection, the GPS fix. This is the one link that can be an office network
away rather than a wire on the bench, and it's also the one link that can
be down — which is the whole subject of the next section.

**A shared secret, camera → backend directly, bypassing the Pi.** The two
ESP32-CAMs normally stream to the Pi (MJPEG over the LAN, relayed into
Site Cameras — see the known `.local` resolution issue in
[Hardware status](#hardware-status)), but "the yard is invisible because
the gate is rebooting" is a bad property for a monitoring feed, so that's
not the only path: with `CCTV_UPLOAD_TOKEN` set, a camera can post a frame
straight to the backend on its own, proving itself with a shared secret
rather than a login. Deliberately *not* a JWT — sign-in and token refresh
on an ESP32 is latency and crash surface a camera doesn't need — and
deliberately scoped to do exactly one thing: this token can replace a
picture and nothing else in the API accepts it, so extracting it from a
camera in the field is a low-value target rather than a way into the
system. Blank by default, so the fallback stays off until someone opts
in. Same idea as [Running with no internet](#running-with-no-internet),
one link narrower: a single point of failure — here, the Pi being off —
shouldn't be able to take out everything downstream of it.

---

## Running with no internet

The premise going in was: a construction site is where the connection is
worst and the checkpoint matters most, so "the backend is unreachable"
had to be a handled case, not an outage.

**Badge lookup falls back to a local cache.** The Pi mirrors the worker
roster from the backend on a timer while it's reachable — not on demand,
because a cache first populated the moment the network dies is useless.
When a badge is scanned and the backend can't be reached, the gate
resolves it from that cache instead of refusing everyone. Verified: with
the backend switched off, `[badge] 0238731604 resolved from the local
cache (2 workers, synced Ns ago)` — the age is shown, so nobody mistakes a
stale cache for a live lookup.

**Detection moves on-device.** With `SAFETYFIRST_ONNX_MODEL` pointed at
the trained weights (`best.onnx`), the gate keeps a copy of the same model
loaded locally via `onnxruntime` — CPU, no accelerator required, no
change to the verdict logic (it's the same rule function the backend
uses, so a worker gets the same answer whichever path ruled on them). By
default (`SAFETYFIRST_INFERENCE=auto`) this is a **standby, not a
replacement**: the backend decides every frame it can reach, and the
on-device model takes over only when it misses, logging the handover both
ways —

```
[inference] backend unreachable — detecting on-device until it returns
[inference] backend is answering again — detection back on the server
```

— and hands back automatically once the backend responds. The retry
interval matters here as much as the fallback itself: a network call
blocks on a socket timeout, so re-asking a dead backend on every single
frame would make the fallback *slower* than having none; the gate instead
waits `SAFETYFIRST_BACKEND_RETRY` seconds (15 by default) between checks
while running locally.

**Decisions made offline are queued, not lost.** A verdict reached with
no backend connection is written to a local SQLite queue
(`pi_app/offline_queue.py`) and retried automatically once the connection
returns.

**Verified together, not just individually** — this is the test that
matters, because a fallback that only works in isolation isn't one: with
the backend pointed at an unreachable address, a real card scan resolved
against the local cache, the local ONNX model produced a real verdict, and
the decision landed in the offline queue — three rows, `granted=0,
missing=["Hardhat","Safety Vest"]`. Restoring the backend connection and
restarting the gate drained the queue to zero on its own; no data was
lost, and no step in the chain needed the network to be up.

**What still needs the network:** signing in for the first time (the
device account's credentials are checked once, cached after), and
anything genuinely new to the site — a worker who was added to the roster
after the last sync isn't in the local cache yet. This is deliberately not
a "full offline mode" for every feature — the admin console, reports, and
anything that reaches multiple sites' data still needs the backend — it's
specifically the one path (badge → verdict → record) that a gate cannot
be allowed to simply stop doing.

**What still needs mains power:** the Pi itself, eventually. None of the
above helps if the gate has no power at all — that's what the UPS HAT is
for, and why it's on this build rather than being an optional extra; see
[Hardware status](#hardware-status). Put together, the design goal is
that neither a dead network link nor a dead mains circuit, on its own,
should be able to stop a badge from being checked.

---

## Hardware status

Everything below is on the actual bench setup — a Raspberry Pi 5, five
ESP32 boards, a GNSS modem, a USB camera — and every claim marked
**tested** was verified against that hardware in a live session, not
inferred from the code. Where a number is quoted (a satellite count, a
resolution, a byte count), it's the number that was actually observed,
including the unflattering ones.

**Raspberry Pi 5 (8GB)** — the site gateway, and the only box with a route
off site. Confirmed via `/proc/device-tree/model`: *Raspberry Pi 5 Model B
Rev 1.1*. Waveshare 10.1" touch display, AI HAT (Hailo, 12 TOPS — not used
for inference; see [Cloud vs. local inference](#cloud-vs-local-inference)
and [Running with no internet](#running-with-no-internet) for what the
on-device fallback uses instead, which is plain CPU ONNX, not the Hailo
accelerator).

**Why there's a UPS HAT: none of this hardware has a battery of its own.**
A Pi, a USB camera, a GPS modem and a USB-connected ESP32 all draw
continuously from mains — cut the power and they don't degrade gracefully,
they just stop, mid-frame, mid-write. The Waveshare UPS HAT (E) (I²C fuel
gauge at `0x2d`, 4×21700 cells, rated 5V/6A out, ~4–5hr on a charge) sits
between the wall and everything hanging off the Pi's own rails
specifically so a site power cut is not the same event as the gate going
dark. Installed and wired in; a live pull-the-power endurance test hasn't
been run on this hardware yet, so the 4–5hr figure is the component's
rated capacity, not a number we've personally timed — flagged that way on
purpose, the same as everywhere else in this document that draws the line
between "designed for" and "watched happen."

**A power cut and a network outage are, in practice, the same failure on
a real site** — the router usually shares the circuit that just went
down. That's exactly the case [Running with no internet](#running-with-no-internet)
is built for: on UPS power with no backend reachable, the gate keeps
reading badges from its local worker cache, keeps ruling PPE on-device
with the same model and the same verdict logic the backend uses, and
queues every decision to be synced the moment either the mains or the
network — usually both — comes back. The UPS's job is narrow and specific:
buy the minutes-to-hours that chain needs to keep working instead of the
checkpoint simply switching off.

**Five ESP32 boards**, all attached and — as of this round — all
**tested live**, not just flashed and assumed working:

| Board | Firmware | Does | Status |
|---|---|---|---|
| Gate master (ESP32 38-pin) | [`gate_master/`](esp32-main/gate_master/) | RC522 badges, ESP-NOW receiver, cooling fan on GPIO25/26, USB serial line to the Pi | **Tested.** Compiled with `arduino-cli` (921,339 bytes, 70% of flash) and flashed to the physical board. Badge scans register and reach the console; fan telemetry reports correctly (`FAN <duty> <rpm>` once per 5s) |
| Sensor node ×2 (ESP32-C3 SuperMini) | [`ppe_sensors/`](esp32-main/ppe_sensors/) | MQ-9 gas + DHT11 temp/humidity, each named by `NODE_ID` | **Tested.** Both nodes reporting live over ESP-NOW through the master — `gate_gas`, `gate_temperature`, `gate_humidity`, `yard_gas`, `yard_temperature`, `yard_humidity`, thousands of readings accumulated and syncing to the backend |
| Camera ×2 (ESP32-CAM) | [`cctv_cam/`](esp32-main/cctv_cam/) | MJPEG + snapshots; one OV2640, one GC2145 | Streaming, relayed through the Pi to Site Cameras in the console |

The sensors sit on their own boards deliberately, off the Pi's GPIO, so
sensor timing never competes with camera and badge work on the same chip.

The two cameras have **different sensors** and it matters: the OV2640
encodes JPEG in hardware, the GC2145 (sold as RHYX-M21-45) has no encoder
at all. One firmware covers both — it tries JPEG, catches the refusal, and
re-initialises in RGB565 with software encoding at a lower resolution.

**The fan is on the master, not the Pi**, because the AI HAT has no
temperature sensor and the Pi's own header is occupied. The Pi sends
`TEMP` down the same wire badges come back on; the master owns the curve,
so a crashed Pi makes the fan speed *up* rather than coast. Confirmed
running at 80% duty, ~850–950 RPM on the bench.

**The USB camera the gate itself uses** (a Generic HD-camera-branded USB
webcam, not one of the two ESP32-CAMs) is **tested**: resolves to
`SAFETYFIRST_CAMERA='HD camera' -> index 0, 640x480`, and the checkpoint
app re-acquires it automatically after being unplugged and replugged —
verified across a full Pi reboot, with no restart of the gate app needed.

**GPS is tested and working, with an honest caveat.** A Vanix TracX-1b
(Quectel EC200U-CN) over USB is found automatically by USB vendor id (see
[Where things stand](#where-things-stand) on device identification),
answers AT commands, and its fix reaches the console — see Site Location
in the admin console, and `pi_app/gps_reporter.py`. The caveat: on the
bench, indoors, it has held a **2-satellite** view (4 are needed for a
fix) — that's an antenna-placement fact, not a software one, and the AT
chain itself (`AT+QGPSLOC`, `AT+QGPSGNMEA`) was confirmed working
end-to-end by querying it directly. Outdoors with a clear sky view this
resolves on its own; nothing in the software needs to change.

**RFID badge reading is tested via the wire that actually carries it**,
which is the gate master's own RC522, not a second reader on the Pi's SPI
header (that path exists as `MFRC522Reader` — a fallback for a gate with
no master attached — but isn't what this build uses). Badge scans were
confirmed working normally *and* with the backend entirely offline,
resolving against the Pi's local worker cache — see
[Running with no internet](#running-with-no-internet).

**ESP-NOW is tested and carrying real traffic.** The master's receive
callback logs every packet; two sensor nodes have been reporting
continuously, accumulating thousands of readings in the Pi's local store
and syncing them to the backend on a timer.

**A known, currently-unresolved issue: the two ESP32-CAMs' `.local`
hostnames don't reliably resolve from the Pi.** `SAFETYFIRST_CCTV_URL`
points the relay at `safetyfirst-cam.local` / `safetyfirst-yard.local`
(mDNS), and at last check the Pi couldn't resolve either — the relay
logged `stream unavailable... Failed to resolve 'safetyfirst-cam.local'`
and Site Cameras showed no tile rather than a frozen or wrong one. This is
specifically the Pi's mDNS resolution, not the cameras — both stream fine
to a laptop on the same LAN. The env var already accepts a plain IP
instead of a `.local` name (`SAFETYFIRST_CCTV_URL=cam=http://192.168.1.51`
— see `pi_app/.env.example`), which is the practical fix until whatever's
wrong with `avahi`/mDNS on that Pi image is tracked down; we're naming it
here rather than letting a judge discover a blank camera tile mid-demo and
wonder if the hardware is the problem.

`pi_app/doctor.py` walks the whole chain — platform, SPI, reader
libraries, camera, backend, credentials, policy, GPS — and says which link
is broken instead of leaving you to guess from a blank screen. It was
itself found to be diagnosing the wrong configuration at one point (it
read the ambient environment instead of the `.env` file the gate actually
runs on, so it could report a setting as fine while the gate ran with
something else entirely) — fixed once identified, and now loads the same
`.env` the checkpoint app does.

**Not yet exercised on this hardware:** a timed, live power-cut test of
the UPS HAT's battery backup (installed, wired, and rated for ~4–5hr — see
above — but not personally clocked on this bench), the UPS's I²C fuel
gauge readout specifically, and reflashing the two sensor-node and two
camera sketches with `arduino-cli` specifically (the gate master sketch
has been, twice — see above). Reflashing over USB on this bench has not
been perfectly reliable: two separate uploads dropped mid-write and
succeeded on an immediate retry, which reads as a marginal USB
link/power condition worth solving before this is anyone's production
gate, not a firmware bug.

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
