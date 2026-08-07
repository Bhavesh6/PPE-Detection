"""Gate endpoints — badge identification and attendance.

Used by the checkpoint device. Separate from detection.py because these are
about *who* is at the gate, not what the camera can see.
"""

from datetime import datetime, time, timezone

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

import alerts
import site_settings
from extensions import db
from models import AttendanceRecord, User

gate_bp = Blueprint("gate", __name__, url_prefix="/api/gate")


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
    last_seen = records[0].timestamp.isoformat() if records else None
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
@jwt_required()
def report_alert():
    """A sensor reporting a site hazard.

    Not admin-gated — same reasoning as /location: this is a device
    reporting a fact, not someone changing a policy. There's no sensor
    hardware wired up yet, so this endpoint currently has two callers in
    practice: the admin console's "Simulate Alert" button (for testing/
    demos) and, once the ESP32-main sensor board exists, that board
    itself — both hit the same endpoint the same way, so nothing here
    changes when the real hardware arrives.
    """
    data = request.get_json(silent=True) or {}
    alert, error = alerts.report(
        data.get("kind"), data.get("severity"),
        message=data.get("message"), source=data.get("source"),
    )
    if error:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "alert": alert}), 201
