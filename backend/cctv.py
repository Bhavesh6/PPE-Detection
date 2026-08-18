"""Relay the site CCTV camera's picture to the console.

The camera (esp32-main/cctv_cam) serves an open HTTP port on the LAN and
holds no credentials. The browser never talks to it directly; this module
sits in between, and that buys three things:

  - Authentication. The camera cannot check who is asking. The console
    already can, so the check lives here, behind @admin_required.
  - Reach. The console is served over HTTPS through a tunnel, and a
    browser will not load an http:// image into an https:// page. Proxying
    means the picture arrives on the same origin as everything else.
  - Containment. Only the backend needs a route to the camera, so the
    camera stays on the LAN instead of being exposed.

Why single frames rather than the camera's MJPEG stream: an <img> tag
cannot send an Authorization header, so proxying the stream for a
long-lived <img src> would mean putting the caller's JWT in the query
string, where it lands in logs and history. Polling /snapshot is a real
cost - a few frames a second instead of fifteen - but it keeps the token
in a header where it belongs. The camera still serves /stream directly to
anyone already on the LAN, which is the right tool for watching it from
the site itself.
"""

import requests
from flask import Blueprint, Response, current_app, jsonify

from admin import admin_required

cctv_bp = Blueprint("cctv", __name__, url_prefix="/api/cctv")

# A camera that has stopped answering must not take a request worker with
# it. These are deliberately short: a frame that arrives four seconds late
# is not a live view, so waiting longer buys nothing anyone wants.
CONNECT_TIMEOUT = 2.0
READ_TIMEOUT = 4.0


def _camera_base() -> str:
    return (current_app.config.get("CCTV_URL") or "").strip().rstrip("/")


@cctv_bp.route("/status", methods=["GET"])
@admin_required
def status():
    """Whether a camera is configured, and whether it currently answers.

    Split deliberately: "nobody has set CCTV_URL" and "it is set but the
    camera is unplugged" need different fixes, and one combined 'no
    camera' message sends people to check the wrong thing.
    """
    base = _camera_base()
    if not base:
        return jsonify({
            "configured": False,
            "reachable": False,
            "message": "No camera configured. Set CCTV_URL in the backend .env.",
        })

    try:
        res = requests.get(f"{base}/snapshot", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        reachable = res.status_code == 200
        message = "Camera is responding." if reachable else f"Camera replied {res.status_code}."
    except requests.RequestException as exc:
        reachable = False
        message = f"Cannot reach the camera at {base} ({type(exc).__name__})."

    return jsonify({"configured": True, "reachable": reachable, "url": base, "message": message})


@cctv_bp.route("/snapshot", methods=["GET"])
@admin_required
def snapshot():
    """One current frame from the camera, as image/jpeg."""
    base = _camera_base()
    if not base:
        return jsonify({"success": False, "message": "No camera configured."}), 503

    try:
        res = requests.get(f"{base}/snapshot", timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as exc:
        # 503 rather than 500: the backend is fine, the camera is not, and
        # the console shows "camera offline" instead of "server error".
        return jsonify({
            "success": False,
            "message": f"Camera unreachable ({type(exc).__name__}).",
        }), 503

    if res.status_code != 200 or not res.content:
        return jsonify({
            "success": False,
            "message": f"Camera returned {res.status_code}.",
        }), 502

    out = Response(res.content, mimetype="image/jpeg")
    # Never cache a frame. A stale one is worse than none, because it
    # looks current and nobody thinks to reload a live view.
    out.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return out
