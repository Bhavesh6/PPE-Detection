import csv
import io
import json
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, jsonify, request, send_file
from flask_jwt_extended import get_jwt_identity, jwt_required

import audit
import evidence
import site_settings
from detection import get_all_states, remove_state
from extensions import db
from models import AttendanceRecord, AuditEvent, DetectionRecord, SensorAlert, User, _iso_utc
from params import int_arg

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


def _user_summary(user, states, last_seen=None):
    state = states.get(str(user.id))
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_guest": user.is_guest,
        "employee_id": user.employee_id,
        "role_title": user.role,
        # admin / operator / guest — what the console filters by. Distinct
        # from `role_title`, which is the job ("Site Engineer").
        "role": "admin" if user.is_admin else ("guest" if user.is_guest else "operator"),
        "created_at": _iso_utc(user.created_at),
        # Derived from the decision record rather than stored on the user:
        # a column would need writing on every frame, and this is read far
        # less often than it would be written.
        "last_seen": _iso_utc(last_seen),
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

    # One grouped query rather than a per-user lookup — this list is polled
    # every few seconds, and N+1 queries here would grow with the roster.
    seen_rows = (
        db.session.query(
            DetectionRecord.user_id,
            db.func.max(DetectionRecord.timestamp),
        )
        .group_by(DetectionRecord.user_id)
        .all()
    )
    last_seen = dict(seen_rows)

    return jsonify({
        "success": True,
        "users": [_user_summary(u, states, last_seen.get(u.id)) for u in users],
    })


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

    # Read before deletion — after the commit there's nothing left to name.
    removed = {
        "name": user.name,
        "email": user.email,
        "employee_id": user.employee_id,
        "was_admin": user.is_admin,
    }
    record_count = DetectionRecord.query.filter_by(user_id=user_id).count()

    DetectionRecord.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    remove_state(str(user_id))

    audit.record(
        audit.USER_DELETED,
        f"Deleted {removed['name']} ({removed['email']}) and {record_count} gate record(s)",
        detail=removed,
    )
    return jsonify({"success": True, "message": "User deleted"})


@admin_bp.route("/violations", methods=["GET"])
@admin_required
def list_violations():
    """Refusals, newest first, with the worker attached."""
    page = int_arg("page", 1, 1, 1_000_000)
    per_page = int_arg("per_page", 20, 1, 100)

    # Expire old images here rather than on a scheduler — this app has no
    # background worker, and the retention promise shouldn't depend on one
    # existing. The filter is indexed and matches nothing on most calls.
    evidence.purge_expired(db.session, DetectionRecord)

    # Refusals and operator snapshots both belong here: they're the records
    # that carry an image and warrant review. `kind` narrows it.
    kind = request.args.get("kind", "all")
    verdicts = {"denied": ["denied"], "snapshot": ["snapshot"]}.get(kind, ["denied", "snapshot"])

    query = (
        DetectionRecord.query.filter(DetectionRecord.verdict.in_(verdicts))
        .order_by(DetectionRecord.timestamp.desc())
    )
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    users = {}
    if rows:
        ids = {r.user_id for r in rows}
        users = {u.id: u for u in User.query.filter(User.id.in_(ids)).all()}

    items = []
    for r in rows:
        user = users.get(r.user_id)
        data = r.to_dict()
        data["worker"] = {
            "id": r.user_id,
            "name": user.name if user else "Unknown",
            "employee_id": (user.employee_id or "") if user else "",
            "role": (user.role or "") if user else "",
            "initials": user.initials if user else "?",
        }
        items.append(data)

    return jsonify({
        "success": True,
        "violations": items,
        "page": page,
        "per_page": per_page,
        "total": total,
    })


@admin_bp.route("/violations/<int:record_id>/evidence", methods=["GET"])
@admin_required
def violation_evidence(record_id):
    """Serve the stored frame for one refusal.

    Routed through the app rather than served statically so the image is
    behind the same admin check as the record — someone's face at a moment
    they were turned away isn't public just because the URL is guessable.
    """
    record = db.session.get(DetectionRecord, record_id)
    if record is None or not record.evidence_file:
        return jsonify({"success": False, "message": "No evidence for this record"}), 404

    path = evidence.path_for(record.evidence_file)
    if path is None:
        return jsonify({"success": False, "message": "Evidence image is no longer stored"}), 404

    return send_file(path, mimetype="image/jpeg")


@admin_bp.route("/audit", methods=["GET"])
@admin_required
def list_audit():
    """The trail of changes to how the gate behaves.

    Read-only by design: there is no endpoint to edit or delete entries,
    because a log an administrator can quietly rewrite proves nothing.
    """
    page = int_arg("page", 1, 1, 1_000_000)
    per_page = int_arg("per_page", 50, 1, 200)

    query = AuditEvent.query.order_by(AuditEvent.timestamp.desc())
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "success": True,
        "events": [e.to_dict() for e in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
    })


@admin_bp.route("/alerts", methods=["GET"])
@admin_required
def list_alerts():
    """Full alert history, cleared ones included — the "logs" view.

    Live status (is anything active right now) is /api/alerts/active,
    reachable by any signed-in user; this is the admin-only record of
    everything that's ever fired.
    """
    page = int_arg("page", 1, 1, 1_000_000)
    per_page = int_arg("per_page", 20, 1, 100)

    query = SensorAlert.query.order_by(SensorAlert.timestamp.desc())
    total = query.count()
    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "success": True,
        "alerts": [a.to_dict() for a in rows],
        "page": page,
        "per_page": per_page,
        "total": total,
    })


@admin_bp.route("/settings", methods=["GET"])
@admin_required
def read_settings():
    """Current checkpoint policy, plus what the model is capable of.

    The capability list ships with the settings so the UI can't offer a
    requirement the model has no class for — a requirement nothing can
    satisfy would refuse everyone, permanently.
    """
    return jsonify({
        "success": True,
        "settings": site_settings.get_all(),
        "detectable_ppe": list(site_settings.DETECTABLE_PPE),
        "defaults": site_settings.DEFAULTS,
    })


@admin_bp.route("/settings", methods=["PUT"])
@admin_required
def write_settings():
    changes = request.get_json(silent=True) or {}
    # Captured before the write, since the audit entry has to say what
    # changed rather than just what it ended up as.
    before = site_settings.get_all()

    settings, error = site_settings.update(changes)
    if error:
        return jsonify({"success": False, "message": error}), 400

    audit.record(
        audit.POLICY_CHANGED,
        audit.describe_policy_change(before, settings),
        detail={"before": before, "after": settings},
    )
    return jsonify({"success": True, "settings": settings})


@admin_bp.route("/location", methods=["GET"])
@admin_required
def read_location():
    """The site's checkpoint location.

    There's no GNSS module wired up yet, so this is a single admin-entered
    point rather than a live fix — set once and left alone until an admin
    changes it, either by hand or from the browser's own location.
    """
    return jsonify({"success": True, "location": site_settings.get("site_location")})


@admin_bp.route("/location", methods=["PUT"])
@admin_required
def write_location():
    """An admin setting the point by hand — the fallback for sites without
    a GPS module yet, and the override if a device ever reports a bad fix.
    """
    body = request.get_json(silent=True) or {}
    before = site_settings.get("site_location")

    after, error = site_settings.set_location(
        body.get("lat"), body.get("lng"), label=body.get("label"), source="manual",
    )
    if error:
        return jsonify({"success": False, "message": error}), 400

    audit.record(
        audit.LOCATION_CHANGED,
        audit.describe_location_change(before, after),
        detail={"before": before, "after": after},
    )
    return jsonify({"success": True, "location": after})


@admin_bp.route("/analytics", methods=["GET"])
@admin_required
def analytics():
    """Turn the raw decision log into things a safety officer can act on.

    A count of violations tells you there's a problem; *which* PPE fails and
    *when* it fails tells you what to change. Everything here is derived from
    DetectionRecord — no estimates, no synthetic figures.
    """
    days = int_arg("days", 30, 1, 365)
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


REPORTS = {
    "gate-activity": {
        "label": "Gate Activity",
        "summary": "Every badge scan: who presented a card, when, and whether they got in.",
    },
    "refusals": {
        "label": "Refusals",
        "summary": "Only entries that were turned away, with what was missing and the policy in force.",
    },
    "worker-compliance": {
        "label": "Worker Compliance",
        "summary": "One row per worker: how often they cleared, how often they were refused, and what they miss most.",
    },
}


def _report_window():
    """Parse ?from=&to= into naive UTC bounds.

    `to` is treated as inclusive of the whole day: a supervisor asking for
    the 5th to the 5th means that day, not the instant it began.
    """
    def parse(value):
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    start = parse(request.args.get("from"))
    end = parse(request.args.get("to"))
    if end:
        end = end + timedelta(days=1)
    return start, end


def _csv_response(rows, header, name, start, end):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)

    span = ""
    if start or end:
        span = f"-{(start or datetime.min):%Y%m%d}-to-{((end - timedelta(days=1)) if end else datetime.now(timezone.utc)):%Y%m%d}"
    filename = f"safetyfirst-{name}{span or f'-{datetime.now(timezone.utc):%Y%m%d-%H%M}'}.csv"

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@admin_bp.route("/reports", methods=["GET"])
@admin_required
def list_reports():
    return jsonify({
        "success": True,
        "reports": [{"id": k, **v} for k, v in REPORTS.items()],
    })


@admin_bp.route("/reports/<report_id>.csv", methods=["GET"])
@admin_required
def export_report(report_id):
    if report_id not in REPORTS:
        return jsonify({"success": False, "message": "Unknown report"}), 404

    start, end = _report_window()

    def windowed(query, column):
        if start:
            query = query.filter(column >= start)
        if end:
            query = query.filter(column < end)
        return query

    if report_id == "gate-activity":
        records = windowed(
            AttendanceRecord.query, AttendanceRecord.timestamp
        ).order_by(AttendanceRecord.timestamp.desc()).all()
        rows = [[
            r.user.name if r.user else "Unknown",
            r.user.email if r.user else "",
            (r.user.employee_id or "") if r.user else "",
            (r.user.role or "") if r.user else "",
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "Granted" if r.granted else "Denied",
            ", ".join(p for p in r.missing_ppe.split(",") if p),
        ] for r in records]
        return _csv_response(
            rows,
            ["Name", "Email", "Employee ID", "Role", "Timestamp", "Result", "Missing PPE"],
            "gate-activity", start, end,
        )

    # Both remaining reports read the durable decision record.
    records = windowed(
        DetectionRecord.query, DetectionRecord.timestamp
    ).order_by(DetectionRecord.timestamp.desc()).all()
    users = {u.id: u for u in User.query.all()}

    if report_id == "refusals":
        rows = []
        for r in records:
            if r.verdict != "denied":
                continue
            user = users.get(r.user_id)
            policy = json.loads(r.policy_json) if r.policy_json else {}
            rows.append([
                user.name if user else "Unknown",
                (user.employee_id or "") if user else "",
                r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                ", ".join(r.missing_ppe.split(",")) if r.missing_ppe else "",
                ", ".join(policy.get("required_ppe", [])) or "not recorded",
                policy.get("confidence_threshold", ""),
                "yes" if r.evidence_file else "no",
            ])
        return _csv_response(
            rows,
            ["Name", "Employee ID", "Timestamp", "Missing PPE",
             "Policy Required", "Confidence Threshold", "Evidence Stored"],
            "refusals", start, end,
        )

    # worker-compliance
    tally = {}
    for r in records:
        if r.verdict not in ("granted", "denied"):
            continue
        t = tally.setdefault(r.user_id, {"granted": 0, "denied": 0, "missing": Counter()})
        t[r.verdict] += 1
        for item in r.missing_ppe.split(","):
            if item:
                t["missing"][item] += 1

    rows = []
    for user_id, t in tally.items():
        user = users.get(user_id)
        decided = t["granted"] + t["denied"]
        worst = t["missing"].most_common(1)
        rows.append([
            user.name if user else "Unknown",
            (user.employee_id or "") if user else "",
            (user.role or "") if user else "",
            t["granted"],
            t["denied"],
            f"{round((t['granted'] / decided) * 100)}%" if decided else "",
            worst[0][0] if worst else "",
        ])
    rows.sort(key=lambda r: r[0].lower())
    return _csv_response(
        rows,
        ["Name", "Employee ID", "Role", "Cleared", "Turned Away",
         "Compliance Rate", "Most Often Missing"],
        "worker-compliance", start, end,
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
