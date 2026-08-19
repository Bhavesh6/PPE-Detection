"""Carry the site camera's picture from the local network to the backend.

The camera (esp32-main/cctv_cam) is wherever the work is — down a shaft,
inside a tunnel, at the far end of a site. It has a local link to this Pi
and nothing else: no route to the internet, and certainly none inbound
from a server sitting in another building. The backend therefore cannot
fetch from it, however healthy the camera is.

So the Pi fetches, and the Pi forwards. That is the same arrangement the
rest of the site already uses — the gate master reaches this Pi over USB,
the ESP-NOW sensor nodes reach the master — and for the same reason: this
box is the one thing on site with a backhaul.

    camera ──LAN──► this relay ──existing API session──► backend

It reuses the gate's own signed-in session rather than opening its own.
The device account is already authenticated and already trusted to report
facts about the site, and a second credential on the same box would be
one more thing to rotate for no gain.

Failure here is never allowed to matter. A camera that is unplugged, out
of range, or still booting simply produces no frame, and the console says
the feed has stopped. Nothing about the gate's own job — badges, PPE,
alerts — depends on this thread doing anything at all.
"""

from __future__ import annotations

import os
import threading
import time

import requests

# These are read when open_relay() is called, never at import time.
#
# checkpoint.py imports this module *before* it calls load_dotenv(), so a
# module-level os.environ read here would see the environment as it stood
# before the .env file was parsed — which is to say, without any of it.
# The relay would then report "none configured" no matter what .env said,
# and the failure is silent: nothing errors, the camera simply never
# appears. gps_reporter.py already reads inside open_gps() for the same
# reason; this now matches it.


def camera_url() -> str:
    """Where the camera lives on the local network, e.g.
    http://safetyfirst-cam.local or http://192.168.1.50 — blank disables
    the relay entirely."""
    return os.environ.get("SAFETYFIRST_CCTV_URL", "").strip().rstrip("/")


def interval() -> float:
    """Seconds between frames. This is a monitoring view, not a
    recording: one frame a second is plenty to see that something is
    happening, and the link out is shared with everything else the gate
    sends."""
    try:
        return float(os.environ.get("SAFETYFIRST_CCTV_INTERVAL", "1.0"))
    except ValueError:
        return 1.0

# The camera is on the LAN and either answers quickly or is not there.
CAMERA_TIMEOUT = 4.0
UPLOAD_TIMEOUT = 10.0


class CCTVRelay:
    """Pulls JPEGs off the local camera and posts them to the backend."""

    def __init__(self, api, url: str, poll: float = 1.0):
        self._api = api
        self._camera = url.rstrip("/")
        self._interval = max(0.2, poll)
        # What the gate prints at startup, so the log says which camera
        # rather than just that there is one.
        self.name = self._camera
        self._running = True
        self._thread: threading.Thread | None = None

        # Enough state for doctor.py and the log to say which half is
        # broken. "The camera is unreachable" and "the backend refused the
        # frame" send you to opposite ends of the site.
        self.frames_sent = 0
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _grab(self) -> bytes | None:
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
                self._send(jpeg)
                self.frames_sent += 1
                if last_reported is not None:
                    print("[cctv] camera feed restored")
                    last_reported = None
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - never kill the gate over a picture
                message = f"{type(exc).__name__}: {exc}"
                self.last_error = message
                if message != last_reported:
                    print(f"[cctv] relay paused — {message}")
                    last_reported = message

            # Measure from the start of the cycle so a slow frame doesn't
            # add its latency to the interval and drift the rate down.
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self._interval - elapsed))


def open_relay(api) -> CCTVRelay | None:
    """Return a started relay, or None when no camera is configured.

    Returning None rather than a do-nothing object is deliberate: the
    caller prints what it got, and "no camera configured" should read
    differently from "a camera that never sends anything".
    """
    url = camera_url()
    if not url:
        return None

    relay = CCTVRelay(api, url, interval())
    relay.start()
    return relay
