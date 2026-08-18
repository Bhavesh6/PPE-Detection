"""SafetyFirst — Raspberry Pi checkpoint application.

A fullscreen gate display. A worker presents a badge, the screen shows who
they are, the camera checks their PPE, and the gate either lets them through
and marks them present or tells them exactly what to put on.

Flow
    IDLE      waiting for a badge
    PROFILE   badge recognised — showing who this is, PPE check running
    GRANTED   cleared; attendance recorded; returns to IDLE on its own
    DENIED    turned away; Retry, or clear the gate for the next person

Running natively rather than in a browser removes the constraints a web page
carries at a gate: no secure-origin requirement for camera access, no cache,
no kiosk flags — and direct access to the RFID module over SPI.

    python checkpoint.py

Configuration comes from the environment (a .env beside this file works too):

    SAFETYFIRST_API         API base URL          (default http://localhost:5000)
    SAFETYFIRST_EMAIL       device account email  (optional)
    SAFETYFIRST_PASSWORD    device account passwd (optional)
    SAFETYFIRST_CAMERA      camera index          (default 0)
    SAFETYFIRST_INTERVAL    seconds between sends (default 0.5)
    SAFETYFIRST_READER      auto | mfrc522 | keyboard
    SAFETYFIRST_WINDOWED    set to 1 to disable fullscreen
    SAFETYFIRST_GPS         off | auto | serial     (default off — no module yet)
    SAFETYFIRST_GPS_PORT    serial port for the GPS module (default /dev/ttyUSB0)
    SAFETYFIRST_GPS_INTERVAL  seconds between location reports (default 20)
    SAFETYFIRST_QUEUE_FLUSH_INTERVAL  seconds between retrying queued attendance records (default 15)
"""

from __future__ import annotations

import base64
import os
import sqlite3
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import requests
from PIL import Image, ImageTk

import ui
from badge_reader import open_reader
from alert_receiver import start_receiver
from gps_reporter import open_gps
from local_alerts import LocalAlerts
from local_store import LocalStore
from offline_queue import OfflineQueue
from ui import CheckRow, Meter, Type, surface

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass


API_BASE = os.environ.get("SAFETYFIRST_API", "http://localhost:5000").rstrip("/")
EMAIL = os.environ.get("SAFETYFIRST_EMAIL", "")
PASSWORD = os.environ.get("SAFETYFIRST_PASSWORD", "")
# Parsed per-attempt by _camera_candidates(), which accepts an index, a
# comma-separated list, or a name fragment — so this is deliberately not
# int()-ed here, where a name would raise at import and take the gate down.
SEND_INTERVAL = float(os.environ.get("SAFETYFIRST_INTERVAL", "0.5"))
WINDOWED = os.environ.get("SAFETYFIRST_WINDOWED", "") == "1"
GPS_INTERVAL = float(os.environ.get("SAFETYFIRST_GPS_INTERVAL", "20"))
# How often the local mirror refreshes while the cloud is reachable. Often
# enough that a revocation reaches the gate quickly, rare enough that it is
# not meaningful load next to the frame traffic.
ROSTER_SYNC_INTERVAL = float(os.environ.get("SAFETYFIRST_ROSTER_SYNC_INTERVAL", "60"))
# How often a gate that booted offline retries signing in.
SIGNIN_RETRY_INTERVAL = float(os.environ.get("SAFETYFIRST_SIGNIN_RETRY_INTERVAL", "20"))
# Shown while the gate is up but has never reached the backend. Matched
# exactly when clearing it, so a real error raised later isn't wiped.
OFFLINE_MESSAGE = "Offline — no contact with the service"
# LAN receiver so sensors can still report a hazard with the cloud down.
# The token is required: this endpoint can hold the gate, so it must not run
# open. Unset means the receiver stays off entirely.
LOCAL_ALERT_PORT = int(os.environ.get("SAFETYFIRST_LOCAL_ALERT_PORT", "8081"))
LOCAL_ALERT_TOKEN = os.environ.get("SAFETYFIRST_LOCAL_ALERT_TOKEN", "")
# How often locally-raised alerts are retried against the cloud. Shorter
# than the roster sync: a hazard the console hasn't seen is time-sensitive
# in a way a roster refresh is not.
ALERT_REPLAY_INTERVAL = float(os.environ.get("SAFETYFIRST_ALERT_REPLAY_INTERVAL", "10"))
QUEUE_FLUSH_INTERVAL = float(os.environ.get("SAFETYFIRST_QUEUE_FLUSH_INTERVAL", "15"))

REQUEST_TIMEOUT = 20

VIDEO_SPLIT = 0.52
PANEL_PAD = 30

# How long a decision stays on screen before the gate clears itself. A worker
# shouldn't have to press anything for the next person to be served.
GRANTED_HOLD = 5.0
DENIED_HOLD = 12.0      # longer: they need time to read what to put on
# PPE must read clean for this long before the gate opens, so a single lucky
# frame can't clear someone who isn't actually wearing the equipment.
CONFIRM_SECONDS = 1.2
# How long the on-screen exit must be held before the gate closes. Long
# enough that a brush past the screen can't trigger it, short enough that
# it doesn't feel broken to whoever means it.
EXIT_HOLD_MS = 900
# Camera hot-plug. Retry often enough that plugging one in feels immediate,
# and only declare it gone after a run of failed reads — a single dropped
# frame is normal on USB and must not blank a working gate.
CAMERA_RETRY_SECONDS = 2.0
CAMERA_LOST_AFTER_FAILURES = 15

BG = "#070b14"
PANEL = "#0b1120"
INK = "#ffffff"
MUTED = "#94a3b8"
FAINT = "#475569"
AMBER = "#d97706"
OK = "#4ade80"
OK_BG = "#052e16"
BAD = "#f87171"
BAD_BG = "#3f0a0a"
CARD = "#111c30"

BOX_OK = (128, 222, 74)
BOX_PERSON = (233, 179, 96)
BOX_VIOLATION = (113, 113, 248)

IDLE, PROFILE, GRANTED, DENIED = "idle", "profile", "granted", "denied"


@dataclass
class State:
    """Shared between worker threads and the UI; guard with `lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    frame: object = None
    detections: list = field(default_factory=list)
    verdict: str = "no_person"
    missing: list = field(default_factory=list)
    # What this site requires, as reported by the server. Not hardcoded —
    # an administrator can change the policy while the gate is running.
    required: list = field(default_factory=lambda: ["Hardhat", "Safety Vest"])
    connected: bool = False
    message: str = ""
    running: bool = True
    # Attendance records that couldn't reach the backend and are waiting
    # in the local queue for the next successful retry.
    pending_count: int = 0
    # The active critical alert, if any — polled even while idle, so the
    # gate shows "paused" before anyone badges in, not only mid-check.
    active_alert: dict | None = None
    # Whether the local mirror has ever been filled. Nothing depends on it
    # yet; it exists so failover can later tell "cache is cold" apart from
    # "cache says this badge is unknown".
    cache_ready: bool = False
    # Whether the device account has been authenticated. False when the gate
    # booted with no backend and is still retrying.
    signed_in: bool = False

    # gate flow
    mode: str = IDLE
    worker: dict | None = None
    already_present: bool = False
    decided_at: float = 0.0
    clean_since: float = 0.0
    banner: str = ""
    present_today: int = 0


class ApiClient:
    def __init__(self, base_url: str):
        self.base = base_url
        self.session = requests.Session()
        self.token = None

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def sign_in(self) -> tuple[bool, str, bool]:
        """Returns (ok, detail, reachable).

        `reachable` separates the two failures, which need opposite handling:
        a rejected credential is a misconfiguration nobody can fix by waiting,
        while an unreachable backend is the ordinary outage this gate is
        supposed to survive. Collapsing them into one boolean is what made
        the gate refuse to start during an outage.
        """
        if EMAIL and PASSWORD:
            try:
                res = self.session.post(
                    f"{self.base}/api/auth/login",
                    json={"email": EMAIL, "password": PASSWORD}, timeout=10,
                )
                data = res.json()
                if res.ok and data.get("success"):
                    self.token = data["token"]
                    return True, data["user"]["name"], True
            except requests.RequestException:
                return False, "Cannot reach the API", False
            except ValueError:
                # Reached something that isn't the API — a tunnel error page,
                # a captive portal. Answering is not the same as being it.
                return False, "Unexpected response from the API", False
            return False, "Device credentials rejected", True

        try:
            res = self.session.post(f"{self.base}/api/auth/guest", timeout=10)
            data = res.json()
            if res.ok and data.get("success"):
                self.token = data["token"]
                return True, data["user"]["name"], True
            return False, "Guest session refused", True
        except requests.RequestException:
            return False, "Cannot reach the API", False
        except ValueError:
            return False, "Unexpected response from the API", False

    def start(self) -> bool:
        try:
            return self.session.post(
                f"{self.base}/api/start", headers=self._headers(), timeout=10
            ).ok
        except requests.RequestException:
            return False

    def stop(self) -> None:
        try:
            self.session.post(f"{self.base}/api/stop", headers=self._headers(), timeout=5)
        except requests.RequestException:
            pass

    def lookup_badge(self, tag: str) -> tuple[dict | None, bool, str]:
        try:
            res = self.session.get(
                f"{self.base}/api/gate/worker", params={"tag": tag},
                headers=self._headers(), timeout=10,
            )
            data = res.json()
            if res.ok and data.get("success"):
                return data["worker"], data.get("already_present_today", False), ""
            return None, False, data.get("message", "Badge not recognised")
        except requests.RequestException:
            return None, False, "Cannot reach the API"

    def present_today(self) -> int:
        try:
            res = self.session.get(
                f"{self.base}/api/gate/attendance/today",
                headers=self._headers(), timeout=10,
            )
            return res.json().get("present_count", 0) if res.ok else 0
        except requests.RequestException:
            return 0

    def report_alert(self, kind: str, severity: str, message: str, source: str) -> bool:
        """Replay a locally-raised alert. True only if the cloud stored it."""
        try:
            res = self.session.post(
                f"{self.base}/api/gate/alerts",
                json={"kind": kind, "severity": severity,
                      "message": message, "source": source},
                headers=self._headers(), timeout=10,
            )
            return res.ok
        except requests.RequestException:
            return False

    def fetch_roster(self) -> dict | None:
        """Everything needed to rule offline, in one consistent snapshot."""
        try:
            res = self.session.get(
                f"{self.base}/api/gate/roster",
                headers=self._headers(), timeout=20,
            )
            if not res.ok:
                return None
            data = res.json()
            return data if data.get("success") else None
        except (requests.RequestException, ValueError):
            return None

    def mark_attendance(self, user_id: int, granted: bool, missing: list) -> bool:
        try:
            return self.session.post(
                f"{self.base}/api/gate/attendance",
                json={"user_id": user_id, "granted": granted, "missing_ppe": missing},
                headers=self._headers(), timeout=10,
            ).ok
        except requests.RequestException:
            return False

    def active_alerts(self) -> list:
        try:
            res = self.session.get(
                f"{self.base}/api/alerts/active", headers=self._headers(), timeout=10
            )
            if res.ok:
                return res.json().get("alerts", [])
        except requests.RequestException:
            pass
        return []

    def report_location(self, lat: float, lng: float) -> bool:
        try:
            return self.session.post(
                f"{self.base}/api/gate/location",
                json={"lat": lat, "lng": lng},
                headers=self._headers(), timeout=10,
            ).ok
        except requests.RequestException:
            return False

    def send_frame(self, frame) -> dict | None:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            return None
        payload = base64.b64encode(buf).decode()
        try:
            res = self.session.post(
                f"{self.base}/api/socket",
                json={"frame": f"data:image/jpeg;base64,{payload}"},
                headers=self._headers(), timeout=REQUEST_TIMEOUT,
            )
            return res.json() if res.ok else None
        except requests.RequestException:
            return None


def _video_nodes() -> list[tuple[int, str]]:
    """(index, card name) for every capture node, lowest index first.

    Read from sysfs rather than probed by opening each one: opening a device
    to identify it is exactly what must be avoided when another process may
    already have it.
    """
    found = []
    for path in sorted(Path("/sys/class/video4linux").glob("video*")):
        try:
            idx = int(path.name.replace("video", ""))
            name = (path / "name").read_text().strip()
        except (OSError, ValueError):
            continue
        found.append((idx, name))
    return sorted(found)


def _camera_candidates() -> list[int]:
    """Which capture indexes to try, in order.

    SAFETYFIRST_CAMERA accepts an index ("0"), several ("0,2"), or a name
    fragment ("HD camera"). Names matter once more than one camera is
    attached, because indexes are assigned in enumeration order and shift
    when a device is unplugged — pinning the gate to "0" is how it ends up
    staring at the wrong lens after a reboot.
    """
    setting = (os.environ.get("SAFETYFIRST_CAMERA") or "0").strip()

    parts = [p.strip() for p in setting.split(",") if p.strip()]
    if all(p.isdigit() for p in parts):
        return [int(p) for p in parts]

    wanted = setting.lower()
    matches = [idx for idx, name in _video_nodes() if wanted in name.lower()]
    if matches:
        _camera_candidates.warned = False
        return matches

    # Said once, not every retry: this runs on a two-second timer, so an
    # unplugged camera would otherwise write a line a second for as long as
    # the gate is up and bury everything else in the log.
    if not getattr(_camera_candidates, "warned", False):
        print(f"[camera] no device matching {setting!r}; falling back to index 0",
              file=sys.stderr)
        _camera_candidates.warned = True
    return [0]


def _open_camera(index: int):
    """Open a capture device and prove it delivers a frame, else None.

    isOpened() alone is not enough. A camera that needs a vendor daemon to
    start streaming opens cleanly and then times out on every read, so the
    gate would sit holding a device that produces nothing. Reading one frame
    here is what separates "present" from "working", and lets the caller
    move on to the next candidate.

    V4L2 is requested explicitly: left to choose, OpenCV may pick GStreamer,
    whose failed attempts have been seen to leave the device claimed - after
    which every later retry fails against our own stale handle.
    """
    cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None
    return cap


def capture_loop(state: State, api: ApiClient) -> None:
    """Camera at full rate for a live-looking feed; inference throttled.

    The camera is (re)opened from inside the loop rather than once at start,
    so the gate is plug-and-play: it can boot before the camera is plugged in,
    and it recovers on its own if the cable is pulled and put back. Opening
    once and giving up meant a camera attached seconds after launch was never
    picked up, and the screen sat there claiming no camera while a perfectly
    good one was connected — silent until someone thought to restart it.
    """
    cap = None
    read_failures = 0
    last_open_attempt = 0.0
    last_sent = 0.0
    active_index = None

    while True:
        with state.lock:
            if not state.running:
                break
            checking = state.mode == PROFILE

        if cap is None:
            now = time.time()
            if now - last_open_attempt < CAMERA_RETRY_SECONDS:
                time.sleep(0.1)
                continue
            last_open_attempt = now

            # Try each candidate until one actually delivers a frame, so a
            # camera that enumerates but never streams doesn't shut out a
            # working one sitting on the next index.
            for candidate in _camera_candidates():
                cap = _open_camera(candidate)
                if cap is not None:
                    if candidate != active_index:
                        print(f"[camera] using index {candidate}")
                        active_index = candidate
                    break

            if cap is None:
                with state.lock:
                    state.frame = None
                    state.message = "Camera not available"
                continue
            read_failures = 0
            with state.lock:
                # Only clear our own message; a backend error must survive.
                if state.message.startswith("Camera"):
                    state.message = ""

        ok, frame = cap.read()
        if not ok:
            # One bad read is normal; a run of them means the device went away.
            read_failures += 1
            if read_failures >= CAMERA_LOST_AFTER_FAILURES:
                cap.release()
                cap = None
                read_failures = 0
                with state.lock:
                    state.frame = None
                    state.message = "Camera disconnected"
            else:
                time.sleep(0.1)
            continue

        read_failures = 0
        with state.lock:
            state.frame = frame.copy()

        # Only spend inference on someone actually being checked.
        now = time.time()
        if checking and now - last_sent >= SEND_INTERVAL:
            last_sent = now
            result = api.send_frame(frame)
            with state.lock:
                if result is None:
                    state.connected = False
                    state.message = "Lost contact with the detection service"
                else:
                    state.connected = True
                    state.message = ""
                    state.detections = result.get("detections", [])
                    state.verdict = result.get("verdict", "no_person")
                    state.missing = result.get("missing_ppe", [])
                    if result.get("required_ppe"):
                        state.required = result["required_ppe"]

    if cap is not None:
        cap.release()


def record_attendance(state: State, api: ApiClient, queue: OfflineQueue,
                       user_id: int, granted: bool, missing: list) -> None:
    """Record a decision, falling back to the local queue if the backend
    doesn't take it. Runs in its own thread — the gate already moved on to
    GRANTED/DENIED by the time this fires, so it can't block the UI.
    """
    if api.mark_attendance(user_id, granted, missing):
        return
    queue.enqueue(user_id, granted, missing)
    with state.lock:
        state.pending_count = queue.count()


def queue_flush_loop(state: State, api: ApiClient, queue: OfflineQueue) -> None:
    """Retries queued attendance records once the backend is reachable again.

    Stops at the first failure in a cycle rather than trying the rest —
    if the oldest record can't get through, the network is still down and
    the newer ones won't either, so there's no point spending the retry
    budget failing repeatedly. Deliberately no dedup: if a record somehow
    got through right as the connection dropped, replaying it again is a
    harmless double-write, and it's cheaper to accept that than to build
    idempotency for a report field nobody reads twice.
    """
    while True:
        with state.lock:
            if not state.running:
                break
        for row_id, user_id, granted, missing in queue.pending():
            if not api.mark_attendance(user_id, granted, missing):
                break
            queue.discard(row_id)
        with state.lock:
            state.pending_count = queue.count()
        time.sleep(QUEUE_FLUSH_INTERVAL)


def alert_replay_loop(state: State, api: ApiClient, local_alerts) -> None:
    """Push locally-raised alerts to the cloud once it is reachable.

    Stops at the first failure in a cycle, like the attendance queue: if the
    oldest can't get through the network is still down and the rest won't
    either. Order is preserved — a warning followed by a critical must not
    arrive the other way round, since the console reads the latest as the
    current state of the site.
    """
    while True:
        with state.lock:
            if not state.running:
                break

        for row_id, kind, severity, message, source in local_alerts.pending():
            if not api.report_alert(kind, severity, message, source):
                break
            local_alerts.mark_synced(row_id)
            print(f"[alerts] replayed {severity} {kind} to the cloud")

        time.sleep(ALERT_REPLAY_INTERVAL)


def signin_retry_loop(state: State, api: ApiClient) -> None:
    """Keep trying to sign in after booting without a backend.

    Only runs when the gate started offline. Without it, a Pi that rebooted
    during an outage would stay unauthenticated until someone noticed and
    restarted it by hand — which is the same failure as refusing to boot,
    just deferred.
    """
    while True:
        with state.lock:
            if not state.running:
                break

        ok, detail, reachable = api.sign_in()
        if ok:
            api.start()
            with state.lock:
                state.signed_in = True
                state.connected = True
                if state.message == OFFLINE_MESSAGE:
                    state.message = ""
            print(f"Signed in as {detail}")
            return

        if reachable:
            # Reachable but refusing us: waiting cannot fix a bad credential,
            # so say so once rather than logging the same rejection forever.
            print(f"Sign-in still failing: {detail}", file=sys.stderr)

        time.sleep(SIGNIN_RETRY_INTERVAL)


def roster_sync_loop(state: State, api: ApiClient, store) -> None:
    """Keep the local mirror fresh while the cloud is reachable.

    Nothing reads this cache yet — it is filled now so that when failover
    lands it has something to fail over *to*. A cache first populated at the
    moment the network dies is useless, which is the whole reason this runs
    on a timer during normal operation rather than on demand.

    Failures are silent by design: a sync that can't reach the cloud is the
    ordinary case this feature exists for, not an error worth putting on a
    gate display that workers are reading.
    """
    while True:
        with state.lock:
            if not state.running:
                break

        payload = api.fetch_roster()
        if payload is not None:
            try:
                count = store.replace_all(payload)
                with state.lock:
                    state.cache_ready = True
                print(f"[sync] cached {count} workers")
            except (sqlite3.Error, KeyError, TypeError) as exc:
                # A malformed payload must not kill the thread — the old
                # cache stays valid and the next tick tries again.
                print(f"[sync] could not cache roster: {exc}", file=sys.stderr)

        time.sleep(ROSTER_SYNC_INTERVAL)


def gps_loop(state: State, api: ApiClient, gps) -> None:
    """Reports whatever fix the GPS reader currently has, on a timer.

    Independent of badge activity — the gate's position doesn't depend on
    whether someone's being checked. With no module attached this just
    polls a reader that never has a fix, at negligible cost, so there's
    nothing to switch off separately when there's no hardware yet.
    """
    while True:
        with state.lock:
            if not state.running:
                break
        fix = gps.latest()
        if fix is not None:
            api.report_location(*fix)
        time.sleep(GPS_INTERVAL)


def badge_loop(state: State, api: ApiClient, reader, local_alerts) -> None:
    """Turn badge scans into gate sessions."""
    while True:
        with state.lock:
            if not state.running:
                break
            mode = state.mode

        tag = reader.read()
        if tag is None:
            # Refresh the on-site count and any active alert while the gate
            # is idle, so both stay honest without polling during a check —
            # and so a critical alert shows up here even before anyone
            # badges in, not only mid-check.
            if mode == IDLE:
                count = api.present_today()
                active = api.active_alerts()
                # Anything raised locally while the cloud was unreachable
                # counts too, and outranks nothing — a hazard the cloud has
                # not seen yet is still a hazard. Local entries drop out of
                # this list once replayed, so the cloud's own copy takes
                # over rather than the gate holding on two of the same.
                active = list(active) + local_alerts.active()
                critical = next((a for a in active if a.get("severity") == "critical"), None)
                with state.lock:
                    state.present_today = count
                    state.active_alert = critical
                time.sleep(3.0)
            else:
                time.sleep(0.08)
            continue

        # A scan while a decision is on screen clears it and starts the next
        # person — the queue shouldn't wait out a timer.
        worker, already, error = api.lookup_badge(tag)
        with state.lock:
            if worker is None:
                state.banner = error or "Badge not recognised"
                state.decided_at = time.time()
            else:
                state.worker = worker
                state.already_present = already
                state.mode = PROFILE
                state.verdict = "no_person"
                state.missing = []
                state.detections = []
                state.clean_since = 0.0
                state.banner = ""


class CheckpointApp:
    """Fullscreen gate display. Widget updates happen on the UI thread only."""

    def __init__(self, root: tk.Tk, state: State, api: ApiClient, queue: OfflineQueue):
        self.root = root
        self.state = state
        self.api = api
        self.queue = queue
        self._photo = None
        self.type = Type()

        root.title("SafetyFirst Checkpoint")
        root.configure(bg=ui.BG)
        if not WINDOWED:
            root.attributes("-fullscreen", True)
        root.config(cursor="none")
        root.bind("<Escape>", lambda _e: self.shutdown())
        root.bind("<space>", lambda _e: self._clear_gate())

        root.protocol("WM_DELETE_WINDOW", self.shutdown)

        self._build()
        self.root.after(33, self._tick)

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        t = self.type

        # --- top bar
        bar = tk.Frame(self.root, bg=ui.BG, padx=22, pady=11)
        bar.pack(fill="x")
        tk.Label(bar, text="  SafetyFirst  ", bg=ui.AMBER, fg=ui.AMBER_INK,
                 font=t.title).pack(side="left")
        tk.Label(bar, text="  CHECKPOINT", bg=ui.BG, fg=ui.FAINT,
                 font=t.eyebrow).pack(side="left")

        # A way back to the desktop that doesn't need a keyboard. The gate
        # hides the cursor and runs on a wall-mounted touchscreen, so Esc/q
        # alone strands whoever is standing at it. Hold rather than tap: a
        # stray brush against a kiosk must not drop the gate mid-shift.
        self.exit_btn = tk.Label(bar, text="  HOLD TO EXIT  ", bg=ui.BG,
                                 fg=ui.FAINT, font=t.eyebrow)
        self.exit_btn.pack(side="right")
        self.exit_btn.bind("<ButtonPress-1>", self._exit_press)
        self.exit_btn.bind("<ButtonRelease-1>", self._exit_release)
        self._exit_after = None

        self.clock = tk.Label(bar, text="--:--", bg=ui.BG, fg=ui.INK, font=t.mono_lg)
        self.clock.pack(side="right", padx=(0, 18))
        self.status = tk.Label(bar, text="●  CONNECTING", bg=ui.BG, fg=ui.FAINT,
                               font=t.eyebrow)
        self.status.pack(side="right", padx=(0, 18))

        tk.Frame(self.root, bg=ui.LINE, height=1).pack(fill="x")

        body = tk.Frame(self.root, bg=ui.BG)
        body.pack(fill="both", expand=True)

        # place() with relwidth, not pack(expand=True): a Label sizes itself
        # to its image, so with pack the video steals width as the window
        # grows and the verdict gets clipped.
        self.video = tk.Label(body, bg="#000000")
        self.video.place(relx=0, rely=0, relwidth=VIDEO_SPLIT, relheight=1)

        panel = tk.Frame(body, bg=ui.PANEL, padx=PANEL_PAD, pady=18)
        panel.place(relx=VIDEO_SPLIT, rely=0, relwidth=1 - VIDEO_SPLIT, relheight=1)
        self.panel = panel
        panel.bind("<Configure>", self._fit_text)

        # --- identity card
        self.card = surface(panel, bg=ui.CARD)
        inner = tk.Frame(self.card, bg=ui.CARD, padx=13, pady=12)
        inner.pack(fill="both", expand=True)
        self.card_inner = inner

        self.avatar = tk.Label(inner, text="", bg=ui.AMBER, fg=ui.AMBER_INK,
                               width=3, height=1, font=t.avatar)
        self.avatar.pack(side="left", padx=(0, 12))
        ident = tk.Frame(inner, bg=ui.CARD)
        ident.pack(side="left", fill="both", expand=True)
        self.ident = ident
        self.w_name = tk.Label(ident, text="", bg=ui.CARD, fg=ui.INK, anchor="w", font=t.title)
        self.w_name.pack(fill="x")
        self.w_role = tk.Label(ident, text="", bg=ui.CARD, fg=ui.AMBER, anchor="w", font=t.small)
        self.w_role.pack(fill="x")
        self.w_meta = tk.Label(ident, text="", bg=ui.CARD, fg=ui.MUTED, anchor="w", font=t.small)
        self.w_meta.pack(fill="x")

        # --- verdict block
        self.eyebrow = tk.Label(panel, text="", bg=ui.PANEL, fg=ui.FAINT,
                                anchor="w", font=t.eyebrow)
        head = tk.Frame(panel, bg=ui.PANEL)
        self.head = head
        self.glyph = tk.Label(head, text="", bg=ui.PANEL, fg=ui.FAINT, font=t.glyph)
        self.word = tk.Label(head, text="", bg=ui.PANEL, fg=ui.INK,
                             anchor="w", justify="left", font=t.display)
        self.word.pack(side="left")
        self.note = tk.Label(panel, text="", bg=ui.PANEL, fg=ui.MUTED,
                             anchor="w", justify="left", font=t.body)

        # --- requirements
        # Built from whatever the server says this site requires, and rebuilt
        # if that changes, so the gate never lists gear it no longer checks.
        self.checks_box = tk.Frame(panel, bg=ui.PANEL)
        self.checks = {}
        self._checks_for = None
        # Header for the idle-state preview of site policy. Only the waiting
        # screen uses it — once someone is being checked, the rows speak for
        # themselves and a label would just crowd the verdict.
        self.req_head = tk.Label(panel, text="REQUIRED TO PASS", bg=ui.PANEL,
                                 fg=ui.FAINT, anchor="w", font=t.eyebrow)
        self._build_checks(list(self.state.required))

        # --- footer: meter + hint
        foot = tk.Frame(panel, bg=ui.PANEL)
        foot.pack(side="bottom", fill="x")
        self.foot = foot
        self.meter = Meter(foot, bg=ui.PANEL)
        self.meter.pack(fill="x", pady=(0, 7))
        self.hint = tk.Label(foot, text="", bg=ui.PANEL, fg=ui.FAINT,
                             anchor="w", font=t.eyebrow)
        self.hint.pack(fill="x")

    def _build_checks(self, items: list) -> None:
        """(Re)create the requirement rows for the current site policy."""
        if items == self._checks_for:
            return
        for row in self.checks.values():
            row.destroy()
        self.checks = {}
        for item in items:
            row = CheckRow(self.checks_box, item, self.type)
            row.pack(fill="x", pady=4)
            self.checks[item] = row
        self._checks_for = list(items)

    # ---------------------------------------------------------------- layout
    def _fit_text(self, event) -> None:
        self.type.fit(max(event.width - PANEL_PAD * 2, 140))
        self.word.configure(wraplength=max(event.width - PANEL_PAD * 2, 140))
        self.note.configure(wraplength=max(event.width - PANEL_PAD * 2, 140))
        self.w_name.configure(wraplength=max(event.width - PANEL_PAD * 2 - 80, 90))

    def _show(self, widget, **pack) -> None:
        if not widget.winfo_ismapped():
            widget.pack(**pack)

    def _hide(self, widget) -> None:
        if widget.winfo_ismapped():
            widget.pack_forget()

    def _paint(self, bg, card_bg, card_line) -> None:
        """Repaint the panel. Tk backgrounds don't inherit, so every surface
        sitting on the panel has to be told."""
        self.panel.configure(bg=bg)
        for w in (self.eyebrow, self.head, self.word, self.note,
                  self.checks_box, self.req_head, self.foot, self.hint, self.glyph):
            w.configure(bg=bg)
        self.meter.repaint(bg, ui.LINE)
        self.card.configure(bg=card_bg, highlightbackground=card_line,
                            highlightcolor=card_line)
        for w in (self.card_inner, self.ident, self.w_name, self.w_role, self.w_meta):
            w.configure(bg=card_bg)

    # ---------------------------------------------------------------- render
    def _render(self, snap) -> None:
        mode, worker = snap["mode"], snap["worker"]

        if mode == IDLE:
            alert = snap["active_alert"]
            self._paint(ui.HAZ_BG, ui.HAZ_CARD, ui.HAZ_LINE) if alert else self._paint(ui.PANEL, ui.CARD, ui.LINE)
            self._hide(self.card)
            self._hide(self.glyph)
            self._show(self.eyebrow, fill="x", pady=(30, 4))
            self._show(self.head, fill="x")
            self._show(self.note, fill="x", pady=(8, 0))
            if alert:
                # A critical alert overrides the idle screen entirely — no
                # point inviting a badge scan the gate is about to refuse.
                self.eyebrow.configure(text="GATE PAUSED", fg=ui.HAZ)
                self.word.configure(text="Site Alert Active", fg=ui.HAZ)
                self.note.configure(
                    text=f"{alert['kind']}{': ' + alert['message'] if alert.get('message') else ''} — "
                         "wait for an operator to clear it.",
                    fg=ui.HAZ_DIM)
            else:
                self.eyebrow.configure(text="GATE READY", fg=ui.AMBER)
                self.word.configure(text="Scan Your Badge", fg=ui.INK)
                self.note.configure(
                    text=snap["banner"] or "Hold your badge against the reader to begin.",
                    fg=ui.BAD if snap["banner"] else ui.MUTED)
            # Idle is what the gate shows almost all the time, so the panel
            # spends it telling people what they'll be checked against —
            # readable on the walk up, which is the only moment they can
            # still do something about it. Suppressed during an alert: the
            # gate isn't going to accept anyone, so listing gear would only
            # compete with the reason it's paused.
            if alert:
                self._hide(self.req_head)
                self._hide(self.checks_box)
            else:
                self._build_checks(snap["required"])
                if snap["required"]:
                    # checks_box first: coming back from a verdict it is
                    # already packed, and the header has to be re-inserted
                    # above it rather than appended to the end of the panel.
                    self._show(self.checks_box, fill="x")
                    self._show(self.req_head, fill="x", pady=(26, 8),
                               before=self.checks_box)
                    for row in self.checks.values():
                        row.set_state("required")
                else:
                    self._hide(self.req_head)
                    self._hide(self.checks_box)

            self.meter.set(0)
            self.hint.configure(
                text=f"{snap['present_today']} ON SITE TODAY" if snap["present_today"] else "",
                fg=ui.FAINT)
            return

        # identity first, then the ruling — `before` keeps that order, since
        # the verdict labels were packed while idle.
        self._show(self.card, fill="x", pady=(2, 14), before=self.eyebrow)
        if worker:
            self.avatar.configure(text=worker.get("initials", "?"))
            self.w_name.configure(text=worker.get("name", ""))
            self.w_role.configure(text=(worker.get("role") or "Worker").upper())
            bits = [b for b in (worker.get("employee_id"),
                                f"Age {worker['age']}" if worker.get("age") else None) if b]
            self.w_meta.configure(text="   ·   ".join(bits))

        self._show(self.eyebrow, fill="x", pady=(0, 3))
        self._show(self.head, fill="x")
        self._show(self.note, fill="x", pady=(6, 14))
        self._hide(self.req_head)      # idle-only; the rows now carry a ruling
        self._show(self.checks_box, fill="x")

        if mode == PROFILE and snap["verdict"] == "alert_hold":
            # A site alert fired mid-check — not a PPE ruling on this
            # person, so it gets the hazard look, not "denied" red, and
            # _advance() never escalates this to GRANTED or DENIED: it
            # just sits here until the alert clears or someone re-scans.
            alert = snap["active_alert"]
            self._paint(ui.HAZ_BG, ui.HAZ_CARD, ui.HAZ_LINE)
            self._hide(self.glyph)
            self.eyebrow.configure(text="PAUSED", fg=ui.HAZ)
            self.word.configure(text="Entry Paused", fg=ui.HAZ)
            self.note.configure(
                text=(f"{alert['kind']}{': ' + alert['message'] if alert.get('message') else ''} — "
                      "wait for an operator to clear it.")
                if alert else "A site alert is active — wait for an operator to clear it.",
                fg=ui.HAZ_DIM)
            self.meter.set(0)
            self.hint.configure(text="", fg=ui.FAINT)

        elif mode == PROFILE:
            self._paint(ui.PANEL, ui.CARD, ui.LINE)
            self._hide(self.glyph)
            self.eyebrow.configure(text="VERIFYING", fg=ui.AMBER)
            self.word.configure(text="Checking PPE", fg=ui.INK)
            waiting = snap["verdict"] == "no_person"
            self.note.configure(
                text="Step into view of the camera." if waiting
                else "Hold still — confirming your equipment.", fg=ui.MUTED)
            self.meter.set(snap["confirm_progress"], ui.AMBER)
            self.hint.configure(text="", fg=ui.FAINT)

        elif mode == GRANTED:
            self._paint(ui.OK_BG, ui.OK_CARD, ui.OK_LINE)
            # before=word: the verdict label claims the first left slot at
            # build time, so without this the tick/cross trails the text.
            if not self.glyph.winfo_ismapped():
                self.glyph.pack(side="left", padx=(0, 12), before=self.word)
            self.glyph.configure(text="✓", fg=ui.OK)
            self.eyebrow.configure(text="CLEARED", fg=ui.OK_DIM)
            self.word.configure(text="Access Granted", fg=ui.OK)
            self.note.configure(
                text="Already marked present today." if snap["already_present"]
                else "Marked present. You may enter.", fg=ui.OK_DIM)
            self.meter.set(snap["hold_progress"], ui.OK)
            self.hint.configure(text=f"CLEARING IN {snap['hold_left']}s", fg=ui.OK_DIM)

        elif mode == DENIED:
            self._paint(ui.BAD_BG, ui.BAD_CARD, ui.BAD_LINE)
            # before=word: the verdict label claims the first left slot at
            # build time, so without this the tick/cross trails the text.
            if not self.glyph.winfo_ismapped():
                self.glyph.pack(side="left", padx=(0, 12), before=self.word)
            self.glyph.configure(text="✕", fg=ui.BAD)
            self.eyebrow.configure(text="TURNED AWAY", fg=ui.BAD_DIM)
            self.word.configure(text="Access Denied", fg=ui.BAD)
            miss = snap["missing"]
            self.note.configure(
                text=f"Put on your {' and '.join(miss).lower()}, then scan again."
                if miss else "Required equipment not detected.", fg=ui.BAD_DIM)
            self.meter.set(snap["hold_progress"], ui.BAD)
            self.hint.configure(
                text=f"SCAN AGAIN WHEN READY  ·  CLEARS IN {snap['hold_left']}s",
                fg=ui.BAD_DIM)

        # Rebuild first in case an administrator changed the policy since the
        # last frame; set_state below then applies to the current rows.
        self._build_checks(snap["required"])

        for item, row in self.checks.items():
            if mode == PROFILE and snap["verdict"] in ("no_person", "alert_hold"):
                row.set_state("idle")
            else:
                row.set_state("bad" if item in snap["missing"] else "ok")

    def _is_relevant(self, label: str) -> bool:
        """Whether this site's policy cares about a detected class.

        The model reports everything it knows. Boxing a red "NO-Mask" over
        someone's face where masks aren't required tells them they've failed
        a check that doesn't exist here.
        """
        if label == "Person":
            return True
        required = self.state.required
        if not required:
            return True
        item = label[3:] if label.startswith("NO-") else label
        return item in required

    def _draw_boxes(self, frame, detections):
        for det in detections:
            box = det.get("box")
            if not box:
                continue
            x1, y1, x2, y2 = box
            label = det.get("type", "")
            if not self._is_relevant(label):
                continue
            colour = (BOX_VIOLATION if label.startswith("NO-")
                      else BOX_PERSON if label == "Person" else BOX_OK)
            cv2.rectangle(frame, (x1, y1), (x2, y2), colour, 2)
            cv2.putText(frame, f"{label} {det.get('confidence', 0):.0%}",
                        (x1, max(y1 - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        colour, 1, cv2.LINE_AA)
        return frame

    # ---------------------------------------------------------------- flow
    def _clear_gate(self) -> None:
        with self.state.lock:
            self.state.mode = IDLE
            self.state.worker = None
            self.state.missing = []
            self.state.detections = []
            self.state.verdict = "no_person"
            self.state.banner = ""

    def _advance(self) -> None:
        now = time.time()
        with self.state.lock:
            mode = self.state.mode
            verdict = self.state.verdict
            worker = self.state.worker
            missing = list(self.state.missing)

            if mode == PROFILE and worker:
                if verdict == "granted":
                    # Require a sustained pass — one lucky frame shouldn't
                    # clear someone who isn't actually wearing the equipment.
                    if self.state.clean_since == 0.0:
                        self.state.clean_since = now
                    elif now - self.state.clean_since >= CONFIRM_SECONDS:
                        self.state.mode = GRANTED
                        self.state.decided_at = now
                        threading.Thread(target=record_attendance,
                                         args=(self.state, self.api, self.queue, worker["id"], True, []),
                                         daemon=True).start()
                elif verdict == "denied":
                    self.state.clean_since = 0.0
                    self.state.mode = DENIED
                    self.state.decided_at = now
                    threading.Thread(target=record_attendance,
                                     args=(self.state, self.api, self.queue, worker["id"], False, missing),
                                     daemon=True).start()
                else:
                    self.state.clean_since = 0.0

            elif mode == GRANTED and now - self.state.decided_at >= GRANTED_HOLD:
                self.state.mode = IDLE
                self.state.worker = None
            elif mode == DENIED and now - self.state.decided_at >= DENIED_HOLD:
                self.state.mode = IDLE
                self.state.worker = None
            elif mode == IDLE and self.state.banner and now - self.state.decided_at >= 4:
                self.state.banner = ""

    def _tick(self) -> None:
        now = time.time()
        with self.state.lock:
            if not self.state.running:
                return
            mode = self.state.mode
            hold = GRANTED_HOLD if mode == GRANTED else DENIED_HOLD
            # Clamp: a decided_at in the future (clock skew, or a test holding
            # the state) must not render a nonsense countdown at the gate.
            elapsed = max(now - self.state.decided_at, 0.0) if self.state.decided_at else 0.0
            elapsed = min(elapsed, hold)
            confirm = ((now - self.state.clean_since) / CONFIRM_SECONDS
                       if self.state.clean_since else 0.0)
            snap = {
                "frame": None if self.state.frame is None else self.state.frame.copy(),
                "detections": list(self.state.detections),
                "verdict": self.state.verdict,
                "missing": list(self.state.missing),
                "required": list(self.state.required),
                "connected": self.state.connected,
                "message": self.state.message,
                "mode": mode,
                "worker": self.state.worker,
                "already_present": self.state.already_present,
                "banner": self.state.banner,
                "present_today": self.state.present_today,
                "pending_count": self.state.pending_count,
                "active_alert": self.state.active_alert,
                "confirm_progress": min(confirm, 1.0),
                "hold_progress": max(0.0, 1.0 - min(elapsed / hold, 1.0)),
                "hold_left": max(int(hold - elapsed) + 1, 0),
            }

        self._advance()

        frame = snap["frame"]
        if frame is not None:
            if snap["mode"] in (PROFILE, DENIED):
                frame = self._draw_boxes(frame, snap["detections"])
            w, h = self.video.winfo_width(), self.video.winfo_height()
            if w > 10 and h > 10:
                fh, fw = frame.shape[:2]
                scale = min(w / fw, h / fh)
                frame = cv2.resize(frame, (max(int(fw * scale), 1), max(int(fh * scale), 1)))
            self._photo = ImageTk.PhotoImage(
                Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            self.video.configure(image=self._photo)
        elif self._photo is not None:
            # Camera went away — drop the last frame rather than leaving a
            # frozen image that reads as a live feed.
            self._photo = None
            self.video.configure(image="")

        self._render(snap)
        self.clock.configure(text=time.strftime("%H:%M"))
        if snap["message"]:
            self.status.configure(text="●  " + snap["message"].upper(), fg=ui.BAD)
        elif snap["pending_count"]:
            # Surfaced even while connected — a backlog means something was
            # queued during a recent drop and hasn't cleared yet, which an
            # operator should be able to see rather than assume the gate
            # caught everything.
            self.status.configure(text=f"●  LIVE · {snap['pending_count']} SYNCING", fg=ui.AMBER)
        else:
            self.status.configure(text="●  LIVE" if snap["connected"] else "●  READY",
                                  fg=ui.OK if snap["connected"] else ui.FAINT)

        self.root.after(33, self._tick)

    def _exit_press(self, _event=None) -> None:
        """Arm the exit. Held long enough, the gate closes; released early,
        nothing happens — so the control is discoverable without being a
        hazard."""
        self.exit_btn.configure(text="  RELEASE TO CANCEL  ", fg=ui.AMBER)
        self._exit_after = self.root.after(EXIT_HOLD_MS, self.shutdown)

    def _exit_release(self, _event=None) -> None:
        if self._exit_after is not None:
            self.root.after_cancel(self._exit_after)
            self._exit_after = None
        self.exit_btn.configure(text="  HOLD TO EXIT  ", fg=ui.FAINT)

    def shutdown(self) -> None:
        with self.state.lock:
            self.state.running = False
        self.api.stop()
        self.root.after(120, self.root.destroy)


def main() -> int:
    state = State()
    api = ApiClient(API_BASE)

    # A gate that refuses to boot without the backend is useless in exactly
    # the situation offline mode exists for: the power comes back, the Pi
    # restarts, and the network is still down. So an unreachable backend is
    # survivable — the gate comes up and keeps trying in the background.
    #
    # A *rejected* credential is different and still fatal. Waiting cannot
    # fix it, and falling back to a guest session would silently detach the
    # gate from its device account, filing every decision against a
    # throwaway identity. Better to fail where someone will read the reason.
    ok, detail, reachable = api.sign_in()
    if ok:
        print(f"Signed in as {detail}")
        state.signed_in = True
        if not api.start():
            print("Could not start a detection session.", file=sys.stderr)
            return 1
    elif reachable:
        print(f"Sign-in failed: {detail}", file=sys.stderr)
        print("The backend answered and refused these credentials — check "
              "SAFETYFIRST_EMAIL / SAFETYFIRST_PASSWORD.", file=sys.stderr)
        return 1
    else:
        print(f"Starting offline: {detail} ({API_BASE})", file=sys.stderr)
        print("The gate will keep retrying in the background.", file=sys.stderr)
        state.message = OFFLINE_MESSAGE

    # Built before the reader: a serial master reports hazards down the same
    # wire as badges, so it needs somewhere to put them from its first line.
    local_alerts = LocalAlerts()
    waiting = local_alerts.unsynced_count()
    if waiting:
        print(f"{waiting} local alert(s) not yet accepted by the cloud — holding the gate until they are")

    store = LocalStore()
    print(f"Local cache: {store.summary()}")

    reader = open_reader(alerts=local_alerts, policy_provider=store.policy)
    print(f"Badge reader: {reader.name}")
    reader.start()

    gps = open_gps()
    print(f"GPS: {gps.name}")
    gps.start()

    queue = OfflineQueue()
    backlog = queue.count()
    if backlog:
        print(f"{backlog} attendance record(s) waiting from a previous outage — will retry")
    state.pending_count = backlog

    # Sensors can reach the gate directly when the cloud can't be reached.
    # Off unless a token is set: this endpoint can hold the gate, so running
    # it open would let anyone on the network stop the site.
    receiver = start_receiver(
        local_alerts, LOCAL_ALERT_TOKEN, store.policy,
        port=LOCAL_ALERT_PORT,
        # Badges may arrive over the network as well as from the local
        # reader — they land on the same queue, so the gate can't tell (or
        # need to care) which reader a scan came from.
        on_badge=reader.tags.put,
    )
    if receiver:
        print(f"Local alert receiver: listening on :{LOCAL_ALERT_PORT}")
    else:
        print("Local alert receiver: off (set SAFETYFIRST_LOCAL_ALERT_TOKEN to enable)")

    threading.Thread(target=capture_loop, args=(state, api), daemon=True).start()
    threading.Thread(target=badge_loop, args=(state, api, reader, local_alerts), daemon=True).start()
    threading.Thread(target=alert_replay_loop, args=(state, api, local_alerts), daemon=True).start()
    threading.Thread(target=gps_loop, args=(state, api, gps), daemon=True).start()
    threading.Thread(target=queue_flush_loop, args=(state, api, queue), daemon=True).start()
    threading.Thread(target=roster_sync_loop, args=(state, api, store), daemon=True).start()
    if not state.signed_in:
        threading.Thread(target=signin_retry_loop, args=(state, api), daemon=True).start()

    root = tk.Tk()
    CheckpointApp(root, state, api, queue)
    root.mainloop()

    with state.lock:
        state.running = False
    reader.stop()
    gps.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
