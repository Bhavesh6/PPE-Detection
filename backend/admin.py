from functools import wraps

from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required

from detection import get_all_states, get_user_state, remove_state
from extensions import db
from models import User

admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")

EMPTY_COUNTS = {"violations": 0, "helmets": 0, "vests": 0, "gloves": 0}


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
        "created_at": user.created_at.isoformat(),
        "active": state["active"] if state else False,
        "live": state["live"] if state else dict(EMPTY_COUNTS),
        "totals": state["totals"] if state else dict(EMPTY_COUNTS),
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
    state = get_user_state(str(user_id))
    return jsonify({"success": True, "results": state["results"] if state else []})


@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@admin_required
def delete_user(user_id):
    requester_id = int(get_jwt_identity())
    if user_id == requester_id:
        return jsonify({"success": False, "message": "You can't delete your own account"}), 400

    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"success": False, "message": "User not found"}), 404

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

    return jsonify({
        "success": True,
        "total_users": User.query.count(),
        "active_sessions": active_sessions,
        "totals": totals,
    })
