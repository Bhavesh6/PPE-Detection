import csv
import io
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from detection import get_all_states, remove_state
from extensions import db
from models import AttendanceRecord, DetectionRecord, User

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

EMPTY_COUNTS = {"violations": 0, "helmets": 0, "vests": 0, "people": 0}
EMPTY_GATE = {"granted": 0, "denied": 0}


def admin_required(fn):
    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user = db.session.get(User, int(get_jwt_identity()))
        if user is None or not user.is_admin:
            return jsonify({"success": False, "message": "Admin access required"}), 403
        return fn(*args, **kwargs)

    return wrapper


def _user_summary(user, states):
    state = states.get(str(user.id))
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_guest": user.is_guest,
        "created_at": user.created_at.isoformat(),
        "active": state["active"] if state else False,
        "live": state["live"] if state else dict(EMPTY_COUNTS),
        "totals": state["totals"] if state else dict(EMPTY_COUNTS),
        "verdict": state["verdict"] if state else "no_person",
        "gate": state["gate"] if state else dict(EMPTY_GATE),
    }


@admin_bp.route("/users", methods=["GET"])
@admin_required
def list_users():
    users = User.query.order_by(User.created_at.desc()).all()
    states = get_all_states()
    return jsonify({"success": True, "users": [_user_summary(u, states) for u in users]})


@admin_bp.route("/users/<int:user_id>/results", methods=["GET"])
@admin_required
def user_results(user_id):
    records = (
        DetectionRecord.query.filter_by(user_id=user_id)
        .order_by(DetectionRecord.timestamp.desc())
        .limit(50)
        .all()
    )
    results = [
        {"timestamp": r.timestamp.strftime("%H:%M:%S"), "detections": r.to_dict()["detections"]}
        for r in records
    ]
    return jsonify({"success": True, "results": results})


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@admin_required
def delete_user(user_id):
    requester_id = int(get_jwt_identity())
    if user_id == requester_id:
        return jsonify({"success": False, "message": "You can't delete your own account"}), 400

    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"success": False, "message": "User not found"}), 404

    DetectionRecord.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    remove_state(str(user_id))
    return jsonify({"success": True, "message": "User deleted"})


@admin_bp.route("/analytics", methods=["GET"])
@admin_required
def analytics():
    """Turn the raw decision log into things a safety officer can act on.

    A count of violations tells you there's a problem; *which* PPE fails and
    *when* it fails tells you what to change. Everything here is derived from
    DetectionRecord — no estimates, no synthetic figures.
    """
    days = min(max(int(request.args.get("days", 30)), 1), 365)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    def summarize(rows):
        granted = sum(1 for r in rows if r.verdict == "granted")
        denied = sum(1 for r in rows if r.verdict == "denied")
        decided = granted + denied
        return {
            "granted": granted,
            "denied": denied,
            "total": len(rows),
            "compliance_rate": round((granted / decided) * 100, 1) if decided else None,
        }

    # Naive timestamps are stored, so compare against naive bounds.
    current_rows = DetectionRecord.query.filter(
        DetectionRecord.timestamp >= start.replace(tzinfo=None)
    ).all()
    previous_rows = DetectionRecord.query.filter(
        DetectionRecord.timestamp >= prev_start.replace(tzinfo=None),
        DetectionRecord.timestamp < start.replace(tzinfo=None),
    ).all()

    current = summarize(current_rows)
    previous = summarize(previous_rows)

    # Which requirement actually fails, and how often. This is the number
    # that tells someone what to fix.
    missing_counter = Counter()
    for r in current_rows:
        for item in r.missing_ppe.split(","):
            if item:
                missing_counter[item] += 1
    missing_total = sum(missing_counter.values())
    missing_breakdown = [
        {
            "item": item,
            "count": count,
            "percent": round((count / missing_total) * 100, 1) if missing_total else 0,
        }
        for item, count in missing_counter.most_common()
    ]

    # When do denials cluster? Shift changes and end-of-day look very
    # different from a flat distribution.
    by_hour = [0] * 24
    for r in current_rows:
        if r.verdict == "denied":
            by_hour[r.timestamp.hour] += 1

    # Daily trend, zero-filled so gaps read as "no activity" rather than
    # silently collapsing the axis.
    daily = {}
    for i in range(days):
        key = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        daily[key] = {"date": key, "granted": 0, "denied": 0}
    for r in current_rows:
        key = r.timestamp.strftime("%Y-%m-%d")
        if key in daily and r.verdict in ("granted", "denied"):
            daily[key][r.verdict] += 1

    # Per-worker scorecard — a column per requirement beats one flat rate.
    per_worker = {}
    for r in current_rows:
        if r.verdict not in ("granted", "denied"):
            continue
        w = per_worker.setdefault(r.user_id, {"granted": 0, "denied": 0, "missing": Counter()})
        w[r.verdict] += 1
        for item in r.missing_ppe.split(","):
            if item:
                w["missing"][item] += 1

    users = {u.id: u for u in User.query.filter(User.id.in_(per_worker.keys())).all()} if per_worker else {}
    workers = []
    for uid, w in per_worker.items():
        user = users.get(uid)
        decided = w["granted"] + w["denied"]
        workers.append({
            "user_id": uid,
            "name": user.name if user else "Unknown",
            "employee_id": (user.employee_id or "") if user else "",
            "role": (user.role or "") if user else "",
            "granted": w["granted"],
            "denied": w["denied"],
            "compliance_rate": round((w["granted"] / decided) * 100) if decided else None,
            "missing": dict(w["missing"]),
        })
    workers.sort(key=lambda x: (x["compliance_rate"] if x["compliance_rate"] is not None else 101))

    def delta(cur, prev):
        if prev in (None, 0) or cur is None:
            return None
        return round(((cur - prev) / prev) * 100, 1)

    return jsonify({
        "success": True,
        "days": days,
        "current": current,
        "previous": previous,
        "deltas": {
            "total": delta(current["total"], previous["total"]),
            "denied": delta(current["denied"], previous["denied"]),
            "compliance_rate": (
                round(current["compliance_rate"] - previous["compliance_rate"], 1)
                if current["compliance_rate"] is not None and previous["compliance_rate"] is not None
                else None
            ),
        },
        "missing_breakdown": missing_breakdown,
        "by_hour": by_hour,
        "daily": list(daily.values()),
        "workers": workers,
    })


@admin_bp.route("/export/attendance.csv", methods=["GET"])
@admin_required
def export_attendance_csv():
    """The report a site supervisor actually asks for: who was at the gate,
    when, and whether they were let in — one row per badge scan."""
    records = AttendanceRecord.query.order_by(AttendanceRecord.timestamp.desc()).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Name", "Email", "Employee ID", "Role", "Timestamp", "Result", "Missing PPE"])
    for r in records:
        user = r.user
        writer.writerow([
            user.name if user else "Unknown",
            user.email if user else "",
            (user.employee_id or "") if user else "",
            (user.role or "") if user else "",
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "Granted" if r.granted else "Denied",
            ", ".join(p for p in r.missing_ppe.split(",") if p),
        ])

    filename = f"safetyfirst-gate-report-{datetime.now(timezone.utc):%Y%m%d-%H%M}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_bp.route("/stats", methods=["GET"])
@admin_required
def site_stats():
    states = get_all_states()
    totals = dict(EMPTY_COUNTS)
    active_sessions = 0

    for state in states.values():
        if state["active"]:
            active_sessions += 1
        for key in totals:
            totals[key] += state["totals"][key]

    # Overview answers "what is happening right now"; /analytics answers
    # "what has been happening over time". Site-wide compliance and all-time
    # decision counts belong there, not here — two tiles with the same name
    # showing different numbers (all-time vs a 30-day window) is worse than
    # showing the figure once.
    today_start = datetime.combine(datetime.now(timezone.utc).date(), time.min)
    today_rows = DetectionRecord.query.filter(DetectionRecord.timestamp >= today_start).all()
    granted_today = sum(1 for r in today_rows if r.verdict == "granted")
    denied_today = sum(1 for r in today_rows if r.verdict == "denied")

    return jsonify({
        "success": True,
        "total_users": User.query.count(),
        "active_sessions": active_sessions,
        "totals": totals,
        "today": {
            "granted": granted_today,
            "denied": denied_today,
            "decisions": granted_today + denied_today,
        },
    })
