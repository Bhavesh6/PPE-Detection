"""Relay the site CCTV camera's picture to the console.

The camera (esp32-main/cctv_cam) serves an open HTTP port and holds no
credentials. The browser never talks to it directly; this module sits in
between, and that buys three things:

  - Authentication. The camera cannot check who is asking. The console
    already can, so the check lives here, behind @admin_required.
  - Reach. The console is served over HTTPS, and a browser will not load
    an http:// image into an https:// page. Proxying puts the picture on
    the same origin as everything else.
  - Containment. Only one machine needs a route to the camera.

Two ways a frame gets here, because the camera is not always somewhere
the server can reach:

  pull   CCTV_URL is set - the server fetches from the camera itself.
         Only possible when both sit on one network.

  relay  CCTV_URL is blank - the Pi POSTs frames to /frame, and the most
         recent one is served from memory. This is the deployment that
         actually matters: a node down a cave or a tunnel has no route to
         anything except the gateway beside it, and the Pi is that
         gateway. It is the same shape as the rest of the system, where
         the gate master reaches the Pi over USB and the ESP-NOW nodes
         reach the master.

Why single frames rather than the camera's MJPEG stream: an <img> tag
cannot send an Authorization header, so serving a stream to a long-lived
<img src> would mean putting the caller's JWT in the query string, where
it lands in logs and history. Polling costs frame rate and keeps the
token in a header. The camera still serves /stream directly to anyone on
its own LAN, which is the right tool for watching it from the site.
"""

import threading
import time

import requests
from flask import Blueprint, Response, current_app, jsonify, request

from admin import admin_required
from gate import device_required

cctv_bp = Blueprint("cctv", __name__, url_prefix="/api/cctv")

# A camera that has stopped answering must not take a request worker with
# it. Deliberately short: a frame four seconds late is not a live view.
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 4.0

# One frame, replaced in place. Never a buffer or a list: the console
# wants the newest picture, and a queue of stale ones would grow without
# bound the moment a viewer is slower than the relay.
_frame_lock = threading.Lock()
_frame: bytes | None = None
_frame_at: float = 0.0

# A relayed frame is only worth showing for so long. Past this the feed
# has stopped and the console should say so rather than present a minutes
# old picture as current.
FRAME_STALE_AFTER = 30.0

# The relay is authenticated, but an authenticated device with a bug can
# still send something enormous. VGA JPEG is ~30-60KB; this is generous
# and still bounded.
MAX_FRAME_BYTES = 2 * 1024 * 1024


def _camera_base() -> str:
    return (current_app.config.get("CCTV_URL") or "").strip().rstrip("/")


def _stored_frame() -> tuple[bytes | None, float]:
    with _frame_lock:
        return _frame, _frame_at


@cctv_bp.route("/frame", methods=["POST"])
@device_required
def receive_frame():
    """Accept one JPEG from the Pi's relay.

    device_required, not admin_required: this is a device stating a fact,
    the same standing as a sensor reading. A guest session cannot do it,
    because guest sign-in needs no credentials and this would otherwise
    let anyone who can reach the API paint the console's camera view.
    """
    data = request.get_data(cache=False)
    if not data:
        return jsonify({"success": False, "message": "Empty frame"}), 400
    if len(data) > MAX_FRAME_BYTES:
        return jsonify({"success": False, "message": "Frame too large"}), 413

    global _frame, _frame_at
    with _frame_lock:
        _frame = data
        _frame_at = time.time()

    return jsonify({"success": True, "bytes": len(data)})


@cctv_bp.route("/status", methods=["GET"])
@admin_required
def status():
    """Whether a camera is configured, and whether a picture is arriving.

    "nobody configured a camera", "it is configured but unreachable" and
    "the relay has gone quiet" need different fixes, so they are reported
    as different things rather than one combined 'no camera'.
    """
    base = _camera_base()

    if base:
        try:
            res = requests.get(f"{base}/snapshot", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            reachable = res.status_code == 200
            message = "Camera is responding." if reachable else f"Camera replied {res.status_code}."
        except requests.RequestException as exc:
            reachable = False
            message = f"Cannot reach the camera at {base} ({type(exc).__name__})."
        return jsonify({
            "configured": True, "mode": "pull", "url": base,
            "reachable": reachable, "message": message,
        })

    frame, at = _stored_frame()
    if frame is None:
        return jsonify({
            "configured": True, "mode": "relay", "url": "via the gate device",
            "reachable": False,
            "message": "Waiting for the gate to relay a frame. Is the checkpoint running, "
                       "and is SAFETYFIRST_CCTV_URL set on the Pi?",
        })

    age = time.time() - at
    fresh = age < FRAME_STALE_AFTER
    return jsonify({
        "configured": True, "mode": "relay", "url": "via the gate device",
        "reachable": fresh,
        "message": "Receiving frames from the gate." if fresh
                   else f"Last frame was {int(age)}s ago — the relay has stopped.",
    })


@cctv_bp.route("/snapshot", methods=["GET"])
@admin_required
def snapshot():
    """One current frame, as image/jpeg."""
    base = _camera_base()

    if base:
        try:
            res = requests.get(f"{base}/snapshot", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as exc:
            # 503 rather than 500: the backend is fine, the camera is not,
            # so the console shows "offline" and not "server error".
            return jsonify({
                "success": False,
                "message": f"Camera unreachable ({type(exc).__name__}).",
            }), 503

        if res.status_code != 200 or not res.content:
            return jsonify({
                "success": False,
                "message": f"Camera returned {res.status_code}.",
            }), 502
        return _jpeg(res.content)

    frame, at = _stored_frame()
    if frame is None:
        return jsonify({"success": False, "message": "No frame relayed yet."}), 503

    # Serve a stale frame as an error rather than a picture. A frozen
    # image looks like a very still room, which is the one way a camera
    # can mislead somebody watching it.
    if (time.time() - at) > FRAME_STALE_AFTER:
        return jsonify({
            "success": False,
            "message": f"Last frame is {int(time.time() - at)}s old.",
        }), 503

    return _jpeg(frame)


def _jpeg(data: bytes) -> Response:
    out = Response(data, mimetype="image/jpeg")
    # Never cache a frame: a cached one is worse than none, because it
    # looks current and nobody thinks to reload a live view.
    out.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return out
