"""A minimal HTTP receiver so sensors can still reach the gate offline.

The ESP32 posts hazards to the cloud. When the cloud is unreachable those
posts fail and the reading is gone — nothing on site ever hears it. This
listens on the LAN and speaks the same two endpoints the board already
calls, so the firmware needs no new protocol: point `API_BASE` at the Pi and
the same requests work.

    POST /api/gate/alerts    {kind, severity, message, source}
    POST /api/gate/sensors   {kind, value, unit, source}

Readings are classified against the site thresholds cached by the roster
sync, so the same ppm that raises a critical alert online raises one here.

**Authentication is required, not optional.** This endpoint can hold the
gate shut, so an open port would let anyone on the network stop the site.
With no token configured the receiver refuses to start rather than run
unauthenticated — a sensor that cannot report is a visible failure, while a
gate anyone can freeze is an invisible one.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY_BYTES = 8192          # these payloads are a few hundred bytes


class _Handler(BaseHTTPRequestHandler):
    # Injected by start_receiver().
    store = None
    token = ""
    policy_provider = None
    on_alert = None

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):        # noqa: A003 - stdlib hook
        """Silence the default stderr access log.

        This runs behind a gate display; a line per sensor poll would bury
        the messages that matter in the app's own output.
        """

    def _reply(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        expected = type(self).token
        supplied = self.headers.get("X-Device-Token", "")
        # compare_digest rather than == so a wrong token can't be recovered
        # byte-by-byte from response timing. Cheap, so no reason not to.
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def do_POST(self):                        # noqa: N802 - stdlib hook
        if not self._authorised():
            self._reply(403, {"success": False, "message": "Bad or missing device token"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._reply(400, {"success": False, "message": "Bad Content-Length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._reply(400, {"success": False, "message": "Body missing or too large"})
            return

        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            self._reply(400, {"success": False, "message": "Body is not JSON"})
            return
        if not isinstance(data, dict):
            self._reply(400, {"success": False, "message": "Body must be an object"})
            return

        cls = type(self)
        if self.path.rstrip("/") == "/api/gate/alerts":
            kind = (data.get("kind") or "").strip()
            severity = (data.get("severity") or "").strip()
            if not kind or severity not in ("critical", "warning", "info"):
                self._reply(400, {"success": False, "message": "kind and a valid severity are required"})
                return
            alert = cls.store.record(kind, severity, data.get("message") or "",
                                     data.get("source") or "esp32")
            if cls.on_alert:
                cls.on_alert()
            self._reply(201, {"success": True, "alert": alert})
            return

        if self.path.rstrip("/") == "/api/gate/sensors":
            kind = (data.get("kind") or "").strip()
            try:
                value = float(data.get("value"))
            except (TypeError, ValueError):
                self._reply(400, {"success": False, "message": "value must be a number"})
                return
            if not kind:
                self._reply(400, {"success": False, "message": "kind is required"})
                return

            from local_alerts import evaluate
            thresholds = (cls.policy_provider() or {}).get("sensor_thresholds") or {}
            severity, _cfg = evaluate(kind, value, thresholds)
            if severity is None:
                # Logged nowhere offline, but not an error: online this is the
                # "no threshold configured" case, which also raises nothing.
                self._reply(201, {"success": True, "alert": None, "severity": None})
                return

            alert = cls.store.record(
                kind, severity,
                f"{kind} reading {value}{data.get('unit') or ''}",
                data.get("source") or "esp32", value=value,
            )
            if cls.on_alert:
                cls.on_alert()
            self._reply(201, {"success": True, "alert": alert, "severity": severity})
            return

        self._reply(404, {"success": False, "message": "No such endpoint"})

    def do_GET(self):                         # noqa: N802 - stdlib hook
        """Liveness only — so a sensor can tell "Pi is up" from "wrong IP"."""
        if self.path.rstrip("/") in ("/api/health", "/api/gate/health"):
            self._reply(200, {"success": True, "status": "ok", "service": "gate-local"})
            return
        self._reply(404, {"success": False, "message": "No such endpoint"})


def start_receiver(store, token: str, policy_provider, port: int = 8081,
                   on_alert=None) -> ThreadingHTTPServer | None:
    """Start the LAN receiver, or return None if it must not run.

    Refuses to start without a token — see the module docstring.
    """
    if not token:
        return None

    _Handler.store = store
    _Handler.token = token
    _Handler.policy_provider = staticmethod(policy_provider)
    _Handler.on_alert = staticmethod(on_alert) if on_alert else None

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
