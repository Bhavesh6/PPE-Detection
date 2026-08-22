"""Gate endpoints — badge identification and attendance.

Used by the checkpoint device. Separate from detection.py because these are
about *who* is at the gate, not what the camera can see.
"""

from functools import wraps

from datetime import datetime, time, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

import alerts
import site_settings
from extensions import db, limiter
from models import AttendanceRecord, SensorAlert, User, _iso_utc

gate_bp = Blueprint("gate", __name__, url_prefix="/api/gate")


def device_required(fn):
    """A hazard report or sensor reading needs a real account behind it.

    Guest sign-in (/api/auth/guest) needs no credentials at all — under
    plain @jwt_required(), that meant anyone who could reach the API could
    create a guest session and post a fabricated critical alert, holding
    the gate for everyone with zero authentication. Kept separate from
    admin_required in admin.py: this isn't a policy change, just a device
    stating a fact, so any real (non-guest) account works, not only an
    admin's.
    """
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user = db.session.get(User, int(get_jwt_identity()))
        if user is None or user.is_guest:
            return jsonify({
                "success": False,
                "message": "A guest session cannot report gate alerts or sensor readings — sign in with a real account",
            }), 403
        return fn(*args, **kwargs)

    return wrapper


@gate_bp.route("/worker", methods=["GET"])
@jwt_required()
def lookup_worker():
    """Resolve a badge/RFID tag to a worker profile.

    Tags are compared case-insensitively and trimmed: keyboard-wedge readers
    vary in what they emit, and a stray space shouldn't read as "unknown
    badge" to someone standing at a gate.
    """
    tag = (request.args.get("tag") or "").strip()
    if not tag:
        return jsonify({"success": False, "message": "No badge supplied"}), 400

    user = User.query.filter(db.func.lower(User.rfid_tag) == tag.lower()).first()
    if user is None:
        return jsonify({"success": False, "message": "Badge not recognised"}), 404

    today_start = datetime.combine(datetime.now(timezone.utc).date(), time.min)
    already = (
        AttendanceRecord.query.filter(
            AttendanceRecord.user_id == user.id,
            AttendanceRecord.granted.is_(True),
            AttendanceRecord.timestamp >= today_start,
        ).first()
        is not None
    )

    return jsonify({
        "success": True,
        "worker": user.to_worker_dict(),
        "already_present_today": already,
    })


@gate_bp.route("/attendance", methods=["POST"])
@jwt_required()
def mark_attendance():
    """Record the outcome of a badge scan.

    Denials are recorded too — a worker turned away twice in a week is
    exactly the pattern a supervisor needs to see.
    """
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    granted = bool(data.get("granted"))
    missing = data.get("missing_ppe") or []

    if not user_id:
        return jsonify({"success": False, "message": "user_id is required"}), 400
    if db.session.get(User, int(user_id)) is None:
        return jsonify({"success": False, "message": "Unknown worker"}), 404

    record = AttendanceRecord(
        user_id=int(user_id),
        granted=granted,
        missing_ppe=",".join(missing),
    )
    db.session.add(record)
    db.session.commit()

    return jsonify({"success": True, "record": record.to_dict()}), 201


@gate_bp.route("/attendance/me", methods=["GET"])
@jwt_required()
def attendance_me():
    """The signed-in user's own badge-scan history, for their profile page."""
    user_id = int(get_jwt_identity())
    records = (
        AttendanceRecord.query.filter_by(user_id=user_id)
        .order_by(AttendanceRecord.timestamp.desc())
        .limit(50)
        .all()
    )
    # "Days present" counts distinct calendar dates with a granted scan, not
    # rows — badging in twice in a day is one day on site, not two. Counted
    # over the full history, not just the 50 shown, so it stays accurate for
    # anyone with a longer track record than the recent-activity window.
    all_granted = (
        AttendanceRecord.query.filter_by(user_id=user_id, granted=True)
        .with_entities(AttendanceRecord.timestamp)
        .all()
    )
    days_present = {ts.date() for (ts,) in all_granted}
    last_seen = _iso_utc(records[0].timestamp) if records else None
    return jsonify({
        "success": True,
        "records": [r.to_dict() for r in records],
        "days_present": len(days_present),
        "last_seen": last_seen,
    })


@gate_bp.route("/attendance/today", methods=["GET"])
@jwt_required()
def attendance_today():
    """Everyone who presented a badge today, most recent first."""
    today_start = datetime.combine(datetime.now(timezone.utc).date(), time.min)
    records = (
        AttendanceRecord.query.filter(AttendanceRecord.timestamp >= today_start)
        .order_by(AttendanceRecord.timestamp.desc())
        .limit(200)
        .all()
    )
    present = {r.user_id for r in records if r.granted}
    return jsonify({
        "success": True,
        "records": [r.to_dict() for r in records],
        "present_count": len(present),
    })


@gate_bp.route("/roster", methods=["GET"])
@device_required
@limiter.limit("30 per minute")
def roster():
    """Everything the checkpoint needs to rule on its own while offline.

    One call rather than three, because it is polled on a timer and the three
    pieces have to agree with each other: a roster from one moment and a
    policy from another can produce a verdict that matches neither.

    Includes `rfid_tag`, which `to_worker_dict()` deliberately withholds —
    the gate can't match a badge locally without it. That makes this endpoint
    a badge-list disclosure, which is why it is device-only: a guest session
    (no credentials at all) must never be able to enumerate them.

    Deliberately not paginated. This is the whole roster by definition — a
    partial one would silently turn absent workers into "badge not
    recognised" the moment the network dropped.
    """
    users = User.query.filter(User.is_guest.is_(False)).all()

    today_start = datetime.combine(datetime.now(timezone.utc).date(), time.min)
    present_today = {
        row.user_id
        for row in AttendanceRecord.query.filter(
            AttendanceRecord.granted.is_(True),
            AttendanceRecord.timestamp >= today_start,
        ).all()
    }

    workers = []
    for user in users:
        if not user.rfid_tag:
            continue          # no badge, so nothing the gate could match on
        entry = user.to_worker_dict()
        entry["rfid_tag"] = user.rfid_tag
        entry["already_present_today"] = user.id in present_today
        workers.append(entry)

    # Same set /api/alerts/active serves, so an offline gate pauses on exactly
    # the alerts an online one would.
    unresolved = (
        SensorAlert.query.filter(SensorAlert.acknowledged_at.is_(None))
        .order_by(SensorAlert.timestamp.desc())
        .all()
    )

    return jsonify({
        "success": True,
        "workers": workers,
        "policy": site_settings.get_all(),
        "active_alerts": [row.to_dict() for row in unresolved],
        "present_count": len(present_today),
        # The device stamps its cache with this, not with its own clock: a Pi
        # with no RTC battery boots at the epoch, and an offline record dated
        # 1970 is worse than one dated slightly late.
        "server_time": _iso_utc(datetime.now(timezone.utc)),
    })


@gate_bp.route("/location", methods=["POST"])
@jwt_required()
def report_location():
    """A device reporting its own GPS fix.

    Not admin-gated — this is telemetry from whatever's signed in as the
    gate, not a policy decision. Also not audited: once a module is
    actually reporting, this fires every few seconds, and logging each one
    would bury the handful of admin changes the audit trail exists for.
    Route it through the same set_location() an admin's manual edit uses,
    so both agree on validation and both leave the console's "last updated"
    honest.
    """
    data = request.get_json(silent=True) or {}
    location, error = site_settings.set_location(
        data.get("lat"), data.get("lng"), source="device",
    )
    if error:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "location": location})


@gate_bp.route("/alerts", methods=["POST"])
@device_required
# A malfunctioning or malicious device retrying in a tight loop shouldn't
# be able to flood the alert log — 60/min is 4x the ESP32 sketches' normal
# 4s auto-report cadence (15/min), enough headroom for the 401-retry path
# in postJson() without ever throttling legitimate use.
@limiter.limit("60 per minute")
def report_alert():
    """A sensor reporting a site hazard.

    Admin-gated in spirit, not in code: any real (non-guest) account can
    call this, same as /location — this is a device reporting a fact, not
    someone changing a policy, so it doesn't need to be an admin's account
    specifically, just a real one. In practice this endpoint has two
    callers: the admin console's "Simulate Alert" button, and the ESP32
    sensor board (esp32-main/ppe_sensors) — both hit the same endpoint the
    same way.
    """
    data = request.get_json(silent=True) or {}
    alert, error = alerts.report(
        data.get("kind"), data.get("severity"),
        message=data.get("message"), source=data.get("source"),
    )
    if error:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "alert": alert}), 201


@gate_bp.route("/sensors", methods=["POST"])
@device_required
@limiter.limit("60 per minute")
def report_sensor_reading():
    """A device reporting a raw sensor value (e.g. gas ppm, temperature).

    A step below /alerts: that endpoint is for a device (or the admin
    console's test button) declaring a severity outright, this one is for
    a device that only knows a number and lets the site's configured
    threshold (Alerts page -> Sensor Thresholds) decide whether it's
    nothing, a warning, or critical. A breach raises the exact same alert
    the /alerts path does.
    """
    data = request.get_json(silent=True) or {}
    result, error = alerts.report_reading(
        data.get("kind"), data.get("value"),
        unit=data.get("unit"), source=data.get("source"),
        # When the device measured it, if it says. The gate sends this
        # while replaying readings buffered during an outage; without it
        # a whole outage lands on the history at the second the link
        # came back.
        taken_at=alerts.parse_taken_at(data.get("taken_at")),
    )
    if error:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, **result}), 201
