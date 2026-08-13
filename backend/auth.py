import re
import uuid
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import create_access_token, get_jwt_identity, jwt_required
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from extensions import db, limiter
from models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GUEST_TOKEN_EXPIRES = timedelta(hours=24)


def _validation_error(message):
    return jsonify({"success": False, "message": message}), 400


@auth_bp.route("/signup", methods=["POST"])
# Mass account creation is the abuse this guards against, not a single
# person signing up — 5/hour comfortably covers a real user retrying a
# typo'd password.
@limiter.limit("5 per hour")
def signup():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    if not name:
        return _validation_error("Name is required")
    if not EMAIL_RE.match(email):
        return _validation_error("A valid email is required")
    if len(password) < 8:
        return _validation_error("Password must be at least 8 characters")

    if User.query.filter_by(email=email).first():
        return _validation_error("An account with this email already exists")

    user = User(name=name, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({"success": True, "token": token, "user": user.to_public_dict()}), 201


@auth_bp.route("/login", methods=["POST"])
# Brute-forcing a password is the target here — 10/minute is generous for
# a person who mistyped their own password a few times, hostile to a
# script trying thousands of guesses.
@limiter.limit("10 per minute")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"success": False, "message": "Invalid email or password"}), 401

    token = create_access_token(identity=str(user.id))
    return jsonify({"success": True, "token": token, "user": user.to_public_dict()})


@auth_bp.route("/guest", methods=["POST"])
# Guests can no longer report gate alerts or sensor readings (see
# gate.device_required), so this is no longer the hole it used to be — but
# unlimited guest creation still bloats the users table for no reason, so
# it stays capped.
@limiter.limit("20 per hour")
def guest_login():
    guest_id = uuid.uuid4().hex[:10]
    user = User(
        name=f"Guest-{guest_id[:4]}",
        email=f"guest-{guest_id}@guest.local",
        is_guest=True,
    )
    db.session.add(user)
    db.session.commit()

    token = create_access_token(identity=str(user.id), expires_delta=GUEST_TOKEN_EXPIRES)
    return jsonify({"success": True, "token": token, "user": user.to_public_dict()}), 201


@auth_bp.route("/google", methods=["POST"])
def google_login():
    data = request.get_json(silent=True) or {}
    credential = data.get("credential")
    client_id = current_app.config.get("GOOGLE_CLIENT_ID")

    if not credential:
        return _validation_error("Missing Google credential")
    if not client_id:
        return jsonify({
            "success": False,
            "message": "Google sign-in is not configured on this server (GOOGLE_CLIENT_ID missing)",
        }), 503

    try:
        payload = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id
        )
    except ValueError:
        return jsonify({"success": False, "message": "Invalid Google credential"}), 401

    google_sub = payload["sub"]
    email = (payload.get("email") or "").strip().lower()
    name = payload.get("name") or email.split("@")[0]

    user = User.query.filter_by(google_sub=google_sub).first()
    if user is None:
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(name=name, email=email, google_sub=google_sub)
            db.session.add(user)
        else:
            user.google_sub = google_sub
        db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({"success": True, "token": token, "user": user.to_public_dict()})


@auth_bp.route("/me", methods=["GET"])
@jwt_required()
def me():
    user = db.session.get(User, int(get_jwt_identity()))
    if user is None:
        return jsonify({"success": False, "message": "User not found"}), 404
    return jsonify({"success": True, "user": user.to_public_dict()})
