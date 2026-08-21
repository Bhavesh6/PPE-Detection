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


def interval(camera_id: str | None = None) -> float:
    """Seconds between frames, optionally for one named camera.

    This is a monitoring view, not a recording: one frame a second is
    plenty to see that something is happening, and the link out is shared
    with everything else the gate sends.

    Cameras are not equally fast, and a single global rate makes the
    slowest one set the experience. The OV2640 encodes JPEG in hardware;
    the GC2145 has no encoder and every frame is compressed on the CPU,
    which can take longer than the interval it was asked for. Polling it
    at the fast camera's rate just queues requests it cannot answer.

    So SAFETYFIRST_CCTV_INTERVAL_<ID> overrides the shared
    SAFETYFIRST_CCTV_INTERVAL for one camera:

        SAFETYFIRST_CCTV_INTERVAL=1.0
        SAFETYFIRST_CCTV_INTERVAL_YARD=3.0
    """
    keys = ["SAFETYFIRST_CCTV_INTERVAL"]
    if camera_id:
        slug = re.sub(r"[^A-Z0-9]", "_", camera_id.upper())
        keys.insert(0, f"SAFETYFIRST_CCTV_INTERVAL_{slug}")

    for key in keys:
        raw = os.environ.get(key)
        if raw is None or not raw.strip():
            continue
        try:
            return float(raw)
        except ValueError:
            print(f"[cctv] {key}={raw!r} is not a number — ignoring")
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


def store_dir() -> str:
    """Where to keep frames the backend never received. Blank = don't."""
    return os.environ.get("SAFETYFIRST_CCTV_STORE", "").strip()


def store_budget_mb() -> float:
    try:
        return float(os.environ.get("SAFETYFIRST_CCTV_STORE_MB", "200"))
    except ValueError:
        return 200.0


def store_interval() -> float:
    """Seconds between stored frames while offline.

    Deliberately slower than the relay. Keeping every frame of a
    multi-hour outage is tens of thousands of images that nobody will
    look through, and a full SD card takes the whole gate down to
    preserve footage of an empty yard.
    """
    try:
        return float(os.environ.get("SAFETYFIRST_CCTV_STORE_INTERVAL", "10"))
    except ValueError:
        return 10.0


class FrameStore:
    """Frames kept on disk because the backend could not be reached.

    Not a replay queue, and that is the important distinction. The backend
    holds one current frame per camera — it has no concept of a frame from
    an hour ago, so posting a backlog would simply overwrite the live view
    with history and then be discarded. What an outage actually needs is a
    local record of what the camera saw, which is what this is.

    Bounded by total size, oldest discarded first. A gate that fills its
    own disk stops being a gate.
    """

    def __init__(self, directory: str, budget_mb: float):
        self._dir = directory
        self._budget = int(budget_mb * 1024 * 1024)
        os.makedirs(directory, exist_ok=True)

    def save(self, camera_id: str, jpeg: bytes) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self._dir, f"{camera_id}_{stamp}.jpg")
        try:
            with open(path, "wb") as handle:
                handle.write(jpeg)
            self._prune()
        except OSError as exc:
            # A failed write must not stop the relay retrying the backend:
            # getting the live feed back matters more than the archive.
            print(f"[cctv] could not store frame ({exc})")

    def _prune(self) -> None:
        try:
            files = []
            total = 0
            with os.scandir(self._dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.endswith(".jpg"):
                        size = entry.stat().st_size
                        files.append((entry.stat().st_mtime, entry.path, size))
                        total += size
            if total <= self._budget:
                return
            files.sort()                       # oldest first
            for _mtime, path, size in files:
                if total <= self._budget:
                    break
                os.remove(path)
                total -= size
        except OSError:
            pass


class CCTVRelay:
    """Pulls JPEGs off one local camera and posts them to the backend."""

    def __init__(self, api, camera_id: str, url: str, poll: float = 1.0, store=None):
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
        self.frames_stored = 0
        self.last_error: str | None = None
        self._store = store
        self._store_every = store_interval()
        self._last_stored = 0.0

    @property
    def name(self) -> str:
        # The rate is worth printing: a camera polled at 3s when you
        # expected 1s looks like a laggy feed rather than a setting.
        return f"{self._id} ({self._camera} @ {self._interval:g}s)"

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
                jpeg = self._grab()
                try:
                    self._send(jpeg)
                    self.frames_sent += 1
                except Exception:
                    # The camera answered but the backend did not. Keep the
                    # picture on disk — at the observed rate, not the relay's,
                    # so an outage does not fill the card — then re-raise so
                    # the failure is still reported exactly as before.
                    if self._store is not None:
                        now = time.monotonic()
                        if now - self._last_stored >= self._store_every:
                            self._last_stored = now
                            self._store.save(self._id, jpeg)
                            self.frames_stored += 1
                    raise
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

    @property
    def frames_stored(self) -> int:
        return sum(r.frames_stored for r in self._relays)

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

    # One store across all cameras, so the disk budget is a single
    # number rather than one per camera that quietly multiplies.
    directory = store_dir()
    store = FrameStore(directory, store_budget_mb()) if directory else None
    if store is not None:
        print(f"[cctv] storing offline frames in {directory} "
              f"(max {store_budget_mb():g}MB, one every {store_interval():g}s)")

    group = RelayGroup([
        CCTVRelay(api, camera_id, url, interval(camera_id), store)
        for camera_id, url in targets
    ])
    group.start()
    return group
