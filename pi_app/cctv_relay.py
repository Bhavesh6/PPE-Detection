"""Carry the site cameras' pictures from the local network to the backend.

The cameras (esp32-main/cctv_cam) are wherever the work is — down a
shaft, inside a tunnel, at the far end of a site. They have a local link
to this Pi and nothing else: no route to the internet, and certainly none
inbound from a server sitting in another building. The backend therefore
cannot fetch from them, however healthy they are.

So the Pi fetches, and the Pi forwards. That is the same arrangement the
rest of the site already uses — the gate master reaches this Pi over USB,
the ESP-NOW sensor nodes reach the master — and for the same reason: this
box is the one thing on site with a backhaul.

    camera(s) ──LAN──► this relay ──existing API session──► backend

It reuses the gate's own signed-in session rather than opening its own.
The device account is already authenticated and already trusted to report
facts about the site, and a second credential on the same box would be
one more thing to rotate for no gain.

Each camera gets its own thread. One shared loop would let the slowest
camera set the rate for all of them, and a camera that has been unplugged
blocks until its timeout expires — so a single dead camera would throttle
every healthy one behind it.

Failure here is never allowed to matter. A camera that is unplugged, out
of range, or still booting simply produces no frame, and the console says
that feed has stopped. Nothing about the gate's own job — badges, PPE,
alerts — depends on these threads doing anything at all.

Configuration (SAFETYFIRST_CCTV_URL), comma-separated:

    gate=http://safetyfirst-cam.local,yard=http://192.168.1.51

A bare URL is accepted too; the id is then derived from its hostname.
Naming them explicitly is worth the keystrokes — the id is what labels
the tile in the console, and "yard" reads better than "192-168-1-51".
"""

from __future__ import annotations

import os
import re
import threading
import time
from urllib.parse import urlparse

import requests

# Must satisfy the backend's own id rule, or /frame rejects the post.
_ID_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# The camera is on the LAN and either answers quickly or is not there.
CAMERA_TIMEOUT = 4.0
UPLOAD_TIMEOUT = 10.0


def interval() -> float:
    """Seconds between frames, per camera.

    This is a monitoring view, not a recording: one frame a second is
    plenty to see that something is happening, and the link out is shared
    with everything else the gate sends.
    """
    try:
        return float(os.environ.get("SAFETYFIRST_CCTV_INTERVAL", "1.0"))
    except ValueError:
        return 1.0


def _derive_id(url: str) -> str:
    """Turn a URL into a usable camera id when none was given."""
    host = (urlparse(url).hostname or url).lower()
    if host.endswith(".local"):
        host = host[: -len(".local")]
    slug = re.sub(r"[^a-z0-9_-]", "-", host).strip("-")[:32]
    return slug if _ID_OK.match(slug) else "cam"


def camera_targets() -> list[tuple[str, str]]:
    """Parse SAFETYFIRST_CCTV_URL into (id, url) pairs.

    Read here rather than at import: checkpoint.py imports this module
    before it calls load_dotenv(), so a module-level os.environ read
    would see the environment as it stood before .env was parsed — every
    setting would look unset, silently, with nothing raised.
    """
    raw = os.environ.get("SAFETYFIRST_CCTV_URL", "").strip()
    if not raw:
        return []

    targets: list[tuple[str, str]] = []
    seen: set[str] = set()

    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue

        if "=" in entry and not entry.split("=", 1)[0].startswith("http"):
            name, url = entry.split("=", 1)
            camera_id = name.strip().lower()
        else:
            url = entry
            camera_id = _derive_id(entry)

        url = url.strip().rstrip("/")
        if not url or not _ID_OK.match(camera_id):
            print(f"[cctv] ignoring malformed camera entry: {entry!r}")
            continue

        # Two cameras under one id would overwrite each other's frames in
        # the backend, producing a feed that flickers between two places.
        if camera_id in seen:
            print(f"[cctv] duplicate camera id {camera_id!r} — ignoring {url}")
            continue

        seen.add(camera_id)
        targets.append((camera_id, url))

    return targets


class CCTVRelay:
    """Pulls JPEGs off one local camera and posts them to the backend."""

    def __init__(self, api, camera_id: str, url: str, poll: float = 1.0):
        self._api = api
        self._id = camera_id
        self._camera = url.rstrip("/")
        self._interval = max(0.2, poll)
        self._running = True
        self._thread: threading.Thread | None = None

        # Enough state for doctor.py and the log to say which half is
        # broken. "The camera is unreachable" and "the backend refused the
        # frame" send you to opposite ends of the site.
        self.frames_sent = 0
        self.last_error: str | None = None

    @property
    def name(self) -> str:
        return f"{self._id} ({self._camera})"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _grab(self) -> bytes:
        res = requests.get(f"{self._camera}/snapshot", timeout=CAMERA_TIMEOUT)
        if res.status_code != 200 or not res.content:
            raise RuntimeError(f"camera returned {res.status_code}")
        return res.content

    def _send(self, jpeg: bytes) -> None:
        token = getattr(self._api, "token", None)
        if not token:
            # Not signed in yet, or the session lapsed and the gate's own
            # retry loop hasn't renewed it. Skip: it will be back.
            raise RuntimeError("not signed in")

        res = self._api.session.post(
            f"{self._api.base}/api/cctv/frame",
            params={"id": self._id},
            data=jpeg,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
            timeout=UPLOAD_TIMEOUT,
        )
        if not res.ok:
            raise RuntimeError(f"backend returned {res.status_code}")

    def _loop(self) -> None:
        # Only the first of a run of identical failures is printed. A
        # camera that is off for an hour would otherwise write thousands
        # of identical lines and bury everything the gate actually said.
        last_reported: str | None = None

        while self._running:
            started = time.monotonic()
            try:
                self._send(self._grab())
                self.frames_sent += 1
                if last_reported is not None:
                    print(f"[cctv:{self._id}] feed restored")
                    last_reported = None
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - never kill the gate over a picture
                message = f"{type(exc).__name__}: {exc}"
                self.last_error = message
                if message != last_reported:
                    print(f"[cctv:{self._id}] paused — {message}")
                    last_reported = message

            # Measure from the start of the cycle so a slow frame doesn't
            # add its latency to the interval and drift the rate down.
            time.sleep(max(0.0, self._interval - (time.monotonic() - started)))


class RelayGroup:
    """All configured cameras, started and stopped together."""

    def __init__(self, relays: list[CCTVRelay]):
        self._relays = relays

    @property
    def name(self) -> str:
        return ", ".join(r.name for r in self._relays)

    @property
    def frames_sent(self) -> int:
        return sum(r.frames_sent for r in self._relays)

    def start(self) -> None:
        for relay in self._relays:
            relay.start()

    def stop(self) -> None:
        for relay in self._relays:
            relay.stop()


def open_relay(api) -> RelayGroup | None:
    """Return a started relay group, or None when no camera is configured.

    Returning None rather than an empty group is deliberate: the caller
    prints what it got, and "no camera configured" should read
    differently from "cameras that never send anything".
    """
    targets = camera_targets()
    if not targets:
        return None

    poll = interval()
    group = RelayGroup([CCTVRelay(api, camera_id, url, poll) for camera_id, url in targets])
    group.start()
    return group
