"""Relay the site CCTV cameras' pictures to the console.

The cameras (esp32-main/cctv_cam) serve open HTTP ports and hold no
credentials. The browser never talks to them directly; this module sits
in between, and that buys three things:

  - Authentication. A camera cannot check who is asking. The console
    already can, so the check lives here, behind @admin_required.
  - Reach. The console is served over HTTPS, and a browser will not load
    an http:// image into an https:// page. Proxying puts the picture on
    the same origin as everything else.
  - Containment. Only one machine needs a route to the cameras.

Two ways a frame gets here, because a camera is not always somewhere the
server can reach:

  relay  the Pi POSTs frames to /frame?id=<camera>, and the most recent
         one per camera is served from memory. This is the deployment
         that matters: a node down a shaft or inside a tunnel has no
         route to anything except the gateway beside it, and the Pi is
         that gateway. Same shape as the rest of the site, where the gate
         master reaches the Pi over USB and the ESP-NOW nodes reach the
         master.

  pull   CCTV_URL is set - the server fetches that one camera itself.
         Only possible when both sit on one network, and only ever one
         camera, so it is exposed under the reserved id "local".

Why single frames rather than the cameras' MJPEG streams: an <img> tag
cannot send an Authorization header, so serving a stream to a long-lived
<img src> would mean putting the caller's JWT in the query string, where
it lands in logs and history. Each camera still serves /stream directly
to anyone on its own LAN, which is the right tool for watching it from
the site.

/snapshot remains that polled frame. /stream below is a third option the
choice above missed: fetch() reads a streaming body with the header
intact, so the console can be pushed frames and paint them itself,
without a token ever reaching a URL.
"""

import hmac
import re
import threading
import time
from functools import wraps

import requests
from flask import Blueprint, Response, current_app, jsonify, request

from admin import admin_required
from gate import device_required

cctv_bp = Blueprint("cctv", __name__, url_prefix="/api/cctv")

# A camera that has stopped answering must not take a request worker with
# it. Deliberately short: a frame four seconds late is not a live view.
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 4.0

# One frame per camera, replaced in place. Never a buffer or a list: the
# console wants the newest picture, and a queue of stale ones would grow
# without bound the moment a viewer polls slower than the relay sends.
_frame_lock = threading.Lock()
# id -> (jpeg, received_at, how_it_arrived)
_frames: dict[str, tuple[bytes, float, str]] = {}

# A relayed frame is only worth showing for so long. Past this the feed
# has stopped and the console should say so rather than present a
# minutes-old picture as current.
FRAME_STALE_AFTER = 30.0

# The relay is authenticated, but an authenticated device with a bug can
# still send something enormous. VGA JPEG is ~30-60KB; generous, bounded.
MAX_FRAME_BYTES = 2 * 1024 * 1024

# Bounds on the id space itself. Without these, a device looping over
# generated names would grow the store without limit - each entry pinning
# up to MAX_FRAME_BYTES - and the console would fill with junk tiles.
MAX_CAMERAS = 8
CAMERA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Reserved for the pull-mode camera, so it cannot collide with a relayed
# one of the same name.
PULL_ID = "local"


def _camera_base() -> str:
    return (current_app.config.get("CCTV_URL") or "").strip().rstrip("/")


def device_or_camera_token(fn):
    """Allow a frame from the gate's session, or from a camera directly.

    The relay path is the good one: the Pi holds a real device session and
    the cameras hold no credentials at all. But a camera whose only route
    out is through the Pi disappears entirely when the Pi is off, and "the
    yard is invisible because the gate is rebooting" is a poor property
    for a monitoring feed.

    So a camera may also post on its own, proving itself with a shared
    secret rather than a login. Deliberately not a JWT: sign-in plus token
    refresh on an ESP32 is latency and crashes, and the credential would
    then be worth far more if extracted. This token can do exactly one
    thing — replace a picture — and nothing else in the API accepts it.

    Blank (the default) means the header is refused outright, so the
    fallback stays off until someone opts in. Same convention as
    SAFETYFIRST_LOCAL_ALERT_TOKEN on the Pi, and for the same reason: a
    device endpoint that runs open is a way in.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = (current_app.config.get("CCTV_UPLOAD_TOKEN") or "").strip()
        supplied = (request.headers.get("X-Device-Token") or "").strip()
        # Both must be non-empty: an unset token must never mean "anyone
        # sending an empty header is welcome".
        if expected and supplied and hmac.compare_digest(supplied, expected):
            return fn(*args, **kwargs)
        return device_required(fn)(*args, **kwargs)

    return wrapper


def _clean_id(raw: str | None) -> str | None:
    if not raw:
        return None
    candidate = raw.strip().lower()
    return candidate if CAMERA_ID_RE.match(candidate) else None


def _snapshot_of(camera_id: str) -> tuple[bytes | None, float, str]:
    with _frame_lock:
        return _frames.get(camera_id, (None, 0.0, "none"))


def _known_ids() -> list[str]:
    with _frame_lock:
        return sorted(_frames)


@cctv_bp.route("/frame", methods=["POST"])
@device_or_camera_token
def receive_frame():
    """Accept one JPEG for one camera, from the Pi's relay or the camera.

    A device, not an admin: this is hardware stating a fact, the same
    standing as a sensor reading. A guest session cannot do it, because
    guest sign-in needs no credentials and this would otherwise let
    anyone who can reach the API paint the console's camera view.
    """
    camera_id = _clean_id(request.args.get("id")) or _clean_id(request.headers.get("X-Camera-Id"))
    if camera_id is None:
        return jsonify({"success": False, "message": "Missing or invalid camera id"}), 400
    if camera_id == PULL_ID:
        return jsonify({"success": False, "message": f"'{PULL_ID}' is reserved"}), 400

    data = request.get_data(cache=False)
    if not data:
        return jsonify({"success": False, "message": "Empty frame"}), 400
    if len(data) > MAX_FRAME_BYTES:
        return jsonify({"success": False, "message": "Frame too large"}), 413

    # Worth recording which way it came: a camera that has quietly
    # switched to posting for itself means the gate relay has stopped,
    # and that is a fault the console should be able to show rather than
    # hide behind a picture that still looks fine.
    source = "direct" if request.headers.get("X-Device-Token") else "relay"

    with _frame_lock:
        # Refuse a new id once full rather than evicting one. Evicting
        # would make two misconfigured cameras take turns knocking each
        # other out, which looks like flakiness rather than a limit.
        if camera_id not in _frames and len(_frames) >= MAX_CAMERAS:
            return jsonify({
                "success": False,
                "message": f"Too many cameras (limit {MAX_CAMERAS})",
            }), 429
        _frames[camera_id] = (data, time.time(), source)

    return jsonify({"success": True, "id": camera_id, "bytes": len(data), "via": source})


@cctv_bp.route("/cameras", methods=["GET"])
@admin_required
def cameras():
    """Every camera the console knows about, and whether each is live."""
    now = time.time()
    out = []

    base = _camera_base()
    if base:
        out.append({
            "id": PULL_ID, "mode": "pull", "source": base,
            "online": True, "age": None,
            "message": "Fetched directly by the server.",
        })

    for camera_id in _known_ids():
        _, at, via = _snapshot_of(camera_id)
        age = now - at
        live = age < FRAME_STALE_AFTER
        if live and via == "direct":
            message = "Posting directly — the gate relay is not carrying this one."
        elif live:
            message = "Receiving frames via the gate."
        else:
            message = f"Last frame {int(age)}s ago — this feed has stopped."
        out.append({
            "id": camera_id, "mode": via,
            "source": "the camera itself" if via == "direct" else "via the gate device",
            "online": live, "age": round(age, 1), "message": message,
        })

    return jsonify({"cameras": out, "count": len(out)})


@cctv_bp.route("/status", methods=["GET"])
@admin_required
def status():
    """Whether any camera is configured, and whether pictures are arriving.

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

    ids = _known_ids()
    if not ids:
        return jsonify({
            "configured": True, "mode": "relay", "url": "via the gate device",
            "reachable": False,
            "message": "Waiting for the gate to relay a frame. Is the checkpoint running, "
                       "and is SAFETYFIRST_CCTV_URL set on the Pi?",
        })

    now = time.time()
    live = [i for i in ids if (now - _snapshot_of(i)[1]) < FRAME_STALE_AFTER]
    return jsonify({
        "configured": True, "mode": "relay", "url": "via the gate device",
        "reachable": bool(live),
        "message": (f"Receiving frames from {len(live)} of {len(ids)} camera(s)."
                    if live else "All relayed cameras have gone quiet."),
    })


@cctv_bp.route("/snapshot", methods=["GET"])
@admin_required
def snapshot():
    """One current frame, as image/jpeg.

    ?id= selects a camera. Omitting it returns the only one when there is
    exactly one, so a single-camera install needs no id anywhere.
    """
    requested = _clean_id(request.args.get("id"))
    base = _camera_base()

    if base and (requested in (None, PULL_ID)):
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

    ids = _known_ids()
    if requested is None:
        if len(ids) == 1:
            requested = ids[0]
        elif not ids:
            return jsonify({"success": False, "message": "No frame relayed yet."}), 503
        else:
            return jsonify({
                "success": False,
                "message": f"Several cameras are connected; specify ?id= ({', '.join(ids)}).",
            }), 400

    frame, at, _ = _snapshot_of(requested)
    if frame is None:
        return jsonify({"success": False, "message": f"No frames from '{requested}'."}), 404

    # Serve a stale frame as an error rather than a picture. A frozen
    # image looks like a very still room, which is the one way a camera
    # can actively mislead somebody watching it.
    if (time.time() - at) > FRAME_STALE_AFTER:
        return jsonify({
            "success": False,
            "message": f"Last frame from '{requested}' is {int(time.time() - at)}s old.",
        }), 503

    return _jpeg(frame)


# A streaming response holds its worker thread for as long as it runs, so
# the number of them has to be bounded or a few forgotten tabs starve the
# API everything else depends on. Three covers a console and a spare; the
# next viewer is told to poll instead, which still works.
MAX_LIVE_STREAMS = 3
STREAM_SECONDS = 120.0
STREAM_POLL = 0.05


_stream_lock = threading.Lock()
_live_streams = 0


@cctv_bp.route("/stream", methods=["GET"])
@admin_required
def stream():
    """Frames pushed as they arrive, as multipart/x-mixed-replace.

    Read with fetch() rather than an <img src>, so the Authorization
    header goes with it and no token lands in a URL - see the note at the
    top of this file about why that ruled streaming out before.

    Worth having over polling because the cost per frame drops to a
    boundary and a length, instead of a request, a TLS round trip and a
    response. On the link this runs over - a gateway on a phone hotspot,
    through a tunnel - that overhead was most of the time budget, not the
    picture itself.

    Ends itself after STREAM_SECONDS. The console reconnects; a tab left
    open overnight does not hold a worker until the process restarts.
    """
    requested = _clean_id(request.args.get("id"))
    ids = _known_ids()
    if requested is None:
        if len(ids) == 1:
            requested = ids[0]
        elif not ids:
            return jsonify({"success": False, "message": "No frame relayed yet."}), 503
        else:
            return jsonify({
                "success": False,
                "message": f"Several cameras are connected; specify ?id= ({', '.join(ids)}).",
            }), 400

    global _live_streams
    with _stream_lock:
        if _live_streams >= MAX_LIVE_STREAMS:
            return jsonify({
                "success": False,
                "message": "Too many live viewers right now.",
            }), 503
        _live_streams += 1

    def frames():
        global _live_streams
        try:
            last_at = 0.0
            end = time.monotonic() + STREAM_SECONDS
            while time.monotonic() < end:
                data, at, _how = _snapshot_of(requested)
                # Only on change: resending the same picture would spend
                # exactly the bandwidth this exists to save.
                if data and at > last_at:
                    last_at = at
                    header = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n"
                    )
                    yield header + data + b"\r\n"
                else:
                    time.sleep(STREAM_POLL)
        finally:
            with _stream_lock:
                _live_streams -= 1

    return Response(
        frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


def _jpeg(data: bytes) -> Response:
    out = Response(data, mimetype="image/jpeg")
    # Never cache a frame: a cached one is worse than none, because it
    # looks current and nobody thinks to reload a live view.
    out.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return out
