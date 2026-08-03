from functools import wraps

from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required

from detection import get_all_states, remove_state
from extensions import db
from models import DetectionRecord, User

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

    # Gate decisions come from the permanent record, not memory, so the
    # numbers survive a restart.
    granted = DetectionRecord.query.filter_by(verdict="granted").count()
    denied = DetectionRecord.query.filter_by(verdict="denied").count()
    checked = granted + denied
    compliance_rate = round((granted / checked) * 100) if checked else None

    return jsonify({
        "success": True,
        "total_users": User.query.count(),
        "active_sessions": active_sessions,
        "totals": totals,
        "granted": granted,
        "denied": denied,
        "compliance_rate": compliance_rate,
        "total_records": DetectionRecord.query.count(),
    })
