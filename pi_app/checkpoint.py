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
"""

from __future__ import annotations

import base64
import os
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
from ui import CheckRow, Meter, Type, surface

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass


API_BASE = os.environ.get("SAFETYFIRST_API", "http://localhost:5000").rstrip("/")
EMAIL = os.environ.get("SAFETYFIRST_EMAIL", "")
PASSWORD = os.environ.get("SAFETYFIRST_PASSWORD", "")
CAMERA_INDEX = int(os.environ.get("SAFETYFIRST_CAMERA", "0"))
SEND_INTERVAL = float(os.environ.get("SAFETYFIRST_INTERVAL", "0.5"))
WINDOWED = os.environ.get("SAFETYFIRST_WINDOWED", "") == "1"

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
    connected: bool = False
    message: str = ""
    running: bool = True

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

    def sign_in(self) -> tuple[bool, str]:
        if EMAIL and PASSWORD:
            try:
                res = self.session.post(
                    f"{self.base}/api/auth/login",
                    json={"email": EMAIL, "password": PASSWORD}, timeout=10,
                )
                data = res.json()
                if res.ok and data.get("success"):
                    self.token = data["token"]
                    return True, data["user"]["name"]
            except requests.RequestException:
                return False, "Cannot reach the API"
            return False, "Device credentials rejected"

        try:
            res = self.session.post(f"{self.base}/api/auth/guest", timeout=10)
            data = res.json()
            if res.ok and data.get("success"):
                self.token = data["token"]
                return True, data["user"]["name"]
            return False, "Guest session refused"
        except requests.RequestException:
            return False, "Cannot reach the API"

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

    def mark_attendance(self, user_id: int, granted: bool, missing: list) -> bool:
        try:
            return self.session.post(
                f"{self.base}/api/gate/attendance",
                json={"user_id": user_id, "granted": granted, "missing_ppe": missing},
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


def capture_loop(state: State, api: ApiClient) -> None:
    """Camera at full rate for a live-looking feed; inference throttled."""
    cap = cv2.VideoCapture(CAMERA_INDEX)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not cap.isOpened():
        with state.lock:
            state.message = f"Camera {CAMERA_INDEX} not available"
        return

    last_sent = 0.0
    while True:
        with state.lock:
            if not state.running:
                break
            checking = state.mode == PROFILE

        ok, frame = cap.read()
        if not ok:
            time.sleep(0.1)
            continue

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

    cap.release()


def badge_loop(state: State, api: ApiClient, reader) -> None:
    """Turn badge scans into gate sessions."""
    while True:
        with state.lock:
            if not state.running:
                break
            mode = state.mode

        tag = reader.read()
        if tag is None:
            # Refresh the on-site count while the gate is idle, so the head
            # count on screen stays honest without polling during a check.
            if mode == IDLE:
                count = api.present_today()
                with state.lock:
                    state.present_today = count
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

    def __init__(self, root: tk.Tk, state: State, api: ApiClient):
        self.root = root
        self.state = state
        self.api = api
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

        self.clock = tk.Label(bar, text="--:--", bg=ui.BG, fg=ui.INK, font=t.mono_lg)
        self.clock.pack(side="right")
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
        self.checks_box = tk.Frame(panel, bg=ui.PANEL)
        self.checks = {}
        for item in ("Hardhat", "Safety Vest"):
            row = CheckRow(self.checks_box, item, t)
            row.pack(fill="x", pady=4)
            self.checks[item] = row

        # --- footer: meter + hint
        foot = tk.Frame(panel, bg=ui.PANEL)
        foot.pack(side="bottom", fill="x")
        self.foot = foot
        self.meter = Meter(foot, bg=ui.PANEL)
        self.meter.pack(fill="x", pady=(0, 7))
        self.hint = tk.Label(foot, text="", bg=ui.PANEL, fg=ui.FAINT,
                             anchor="w", font=t.eyebrow)
        self.hint.pack(fill="x")

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
                  self.checks_box, self.foot, self.hint, self.glyph):
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
            self._paint(ui.PANEL, ui.CARD, ui.LINE)
            self._hide(self.card)
            self._hide(self.checks_box)
            self._hide(self.glyph)
            self._show(self.eyebrow, fill="x", pady=(52, 4))
            self._show(self.head, fill="x")
            self._show(self.note, fill="x", pady=(8, 0))
            self.eyebrow.configure(text="GATE READY", fg=ui.AMBER)
            self.word.configure(text="Scan Your Badge", fg=ui.INK)
            self.note.configure(
                text=snap["banner"] or "Hold your badge against the reader to begin.",
                fg=ui.BAD if snap["banner"] else ui.MUTED)
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
        self._show(self.checks_box, fill="x")

        if mode == PROFILE:
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

        for item, row in self.checks.items():
            if mode == PROFILE and snap["verdict"] == "no_person":
                row.set_state("idle")
            else:
                row.set_state("bad" if item in snap["missing"] else "ok")

    def _draw_boxes(self, frame, detections):
        for det in detections:
            box = det.get("box")
            if not box:
                continue
            x1, y1, x2, y2 = box
            label = det.get("type", "")
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
                        threading.Thread(target=self.api.mark_attendance,
                                         args=(worker["id"], True, []), daemon=True).start()
                elif verdict == "denied":
                    self.state.clean_since = 0.0
                    self.state.mode = DENIED
                    self.state.decided_at = now
                    threading.Thread(target=self.api.mark_attendance,
                                     args=(worker["id"], False, missing), daemon=True).start()
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
                "connected": self.state.connected,
                "message": self.state.message,
                "mode": mode,
                "worker": self.state.worker,
                "already_present": self.state.already_present,
                "banner": self.state.banner,
                "present_today": self.state.present_today,
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

        self._render(snap)
        self.clock.configure(text=time.strftime("%H:%M"))
        if snap["message"]:
            self.status.configure(text="●  " + snap["message"].upper(), fg=ui.BAD)
        else:
            self.status.configure(text="●  LIVE" if snap["connected"] else "●  READY",
                                  fg=ui.OK if snap["connected"] else ui.FAINT)

        self.root.after(33, self._tick)

    def shutdown(self) -> None:
        with self.state.lock:
            self.state.running = False
        self.api.stop()
        self.root.after(120, self.root.destroy)


def main() -> int:
    state = State()
    api = ApiClient(API_BASE)

    ok, detail = api.sign_in()
    if not ok:
        print(f"Sign-in failed: {detail}", file=sys.stderr)
        print(f"Is the API running at {API_BASE}?", file=sys.stderr)
        return 1
    print(f"Signed in as {detail}")

    if not api.start():
        print("Could not start a detection session.", file=sys.stderr)
        return 1

    reader = open_reader()
    print(f"Badge reader: {reader.name}")
    reader.start()

    threading.Thread(target=capture_loop, args=(state, api), daemon=True).start()
    threading.Thread(target=badge_loop, args=(state, api, reader), daemon=True).start()

    root = tk.Tk()
    CheckpointApp(root, state, api)
    root.mainloop()

    with state.lock:
        state.running = False
    reader.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
