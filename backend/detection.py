import base64
import json
import threading
from datetime import datetime, timezone

import cv2
import numpy as np
from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from extensions import db
from models import DetectionRecord
from ppe_detection import load_model, process_frame

detection_bp = Blueprint("detection", __name__, url_prefix="/api")

_model = None
_model_lock = threading.Lock()

# Per-user detection state, keyed by user id, so concurrent users/browser
# tabs no longer stomp on one shared set of globals.
_user_state = {}
_state_lock = threading.Lock()

MAX_RESULTS_PER_USER = 50

# PPE the checkpoint requires before it will grant access. The trained model
# emits a matching "NO-<item>" class for each of these when it's missing.
REQUIRED_PPE = ("Hardhat", "Safety Vest")

VERDICT_GRANTED = "granted"
VERDICT_DENIED = "denied"
VERDICT_NO_PERSON = "no_person"


def evaluate_access(detections):
    """Decide whether the gate should open for the person in this frame.

    Returns (verdict, missing_ppe_list). A frame with nobody in it is
    "no_person" rather than a denial, so an empty checkpoint doesn't log
    a stream of false violations.
    """
    present = {d["type"] for d in detections if d.get("detected")}

    if "Person" not in present:
        return VERDICT_NO_PERSON, []

    missing = [
        item for item in REQUIRED_PPE
        if f"NO-{item}" in present or item not in present
    ]
    verdict = VERDICT_DENIED if missing else VERDICT_GRANTED
    return verdict, missing


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = load_model()
    return _model


def _new_state():
    return {
        "active": False,
        "results": [],
        "live": {"violations": 0, "helmets": 0, "vests": 0, "people": 0},
        "totals": {"violations": 0, "helmets": 0, "vests": 0, "people": 0},
        "verdict": VERDICT_NO_PERSON,
        "missing_ppe": [],
        "gate": {"granted": 0, "denied": 0},
    }


def _state_for(user_id):
    with _state_lock:
        if user_id not in _user_state:
            _user_state[user_id] = _new_state()
        return _user_state[user_id]


def get_all_states():
    """Read-only snapshot of every user's detection state, for the admin dashboard."""
    with _state_lock:
        return dict(_user_state)


def get_user_state(user_id):
    """Read-only lookup that does NOT create a new state entry (unlike _state_for)."""
    with _state_lock:
        return _user_state.get(user_id)


def remove_state(user_id):
    with _state_lock:
        _user_state.pop(user_id, None)


def _update_counts(state, detections):
    live = {"violations": 0, "helmets": 0, "vests": 0, "people": 0}

    for detection in detections:
        type_name = detection.get("type", "")
        if not detection.get("detected"):
            continue
        if type_name.startswith("NO-"):
            live["violations"] += 1
            state["totals"]["violations"] += 1
        elif type_name == "Hardhat":
            live["helmets"] += 1
            state["totals"]["helmets"] += 1
        elif type_name == "Safety Vest":
            live["vests"] += 1
            state["totals"]["vests"] += 1
        elif type_name == "Person":
            live["people"] += 1
            state["totals"]["people"] += 1

    state["live"] = live


@detection_bp.route("/start", methods=["POST"])
@jwt_required()
def start_detection():
    user_id = get_jwt_identity()
    # Load the model eagerly so the first frame isn't slowed by a cold load.
    if _get_model() is None:
        return jsonify({"success": False, "message": "Model failed to load"}), 500

    state = _state_for(user_id)
    state["active"] = True
    return jsonify({"success": True, "message": "Detection started"})


@detection_bp.route("/stop", methods=["POST"])
@jwt_required()
def stop_detection():
    user_id = get_jwt_identity()
    state = _state_for(user_id)
    state["active"] = False
    state["verdict"] = VERDICT_NO_PERSON
    state["missing_ppe"] = []
    return jsonify({"success": True, "message": "Detection stopped"})


@detection_bp.route("/status", methods=["GET"])
@jwt_required()
def get_status():
    state = _state_for(get_jwt_identity())
    return jsonify({
        "active": state["active"],
        "violations": state["live"]["violations"],
        "helmets": state["live"]["helmets"],
        "vests": state["live"]["vests"],
        "people": state["live"]["people"],
        "totals": state["totals"],
        "verdict": state["verdict"],
        "missing_ppe": state["missing_ppe"],
        "gate": state["gate"],
        "required_ppe": list(REQUIRED_PPE),
    })


@detection_bp.route("/results", methods=["GET"])
@jwt_required()
def get_results():
    state = _state_for(get_jwt_identity())
    return jsonify({"results": state["results"]})


@detection_bp.route("/history", methods=["GET"])
@jwt_required()
def get_history():
    user_id = int(get_jwt_identity())
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)

    query = (
        DetectionRecord.query.filter_by(user_id=user_id)
        .order_by(DetectionRecord.timestamp.desc())
    )
    total = query.count()
    records = query.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "success": True,
        "records": [r.to_dict() for r in records],
        "page": page,
        "per_page": per_page,
        "total": total,
    })


@detection_bp.route("/socket", methods=["POST"])
@jwt_required()
def process_socket_frame():
    user_id = get_jwt_identity()
    state = _state_for(user_id)

    if not state["active"]:
        return jsonify({"success": False, "message": "Detection is not active"})

    model = _get_model()
    if model is None:
        return jsonify({"success": False, "message": "Model failed to load"}), 500

    try:
        data = request.get_json(silent=True) or {}
        frame_data = data.get("frame", "")

        if "," in frame_data:
            frame_data = frame_data.split(",", 1)[1]

        img_bytes = base64.b64decode(frame_data)
        np_arr = np.frombuffer(img_bytes, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        if frame is None:
            return jsonify({"success": False, "message": "Failed to decode frame"}), 400

        # draw=False: the browser already has the raw frame on <video> and
        # draws its own overlay from the returned box coordinates.
        _, detections = process_frame(frame, model, draw=False)

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        _update_counts(state, detections)

        verdict, missing = evaluate_access(detections)
        previous_verdict = state["verdict"]
        state["verdict"] = verdict
        state["missing_ppe"] = missing

        # A gate decision is an *event*, not a frame. Someone standing at the
        # checkpoint produces the same verdict many times a second; recording
        # each one would bury the real entries under hundreds of duplicates.
        # So we only count and persist when the ruling actually changes.
        is_new_decision = (
            verdict != previous_verdict
            and verdict in (VERDICT_GRANTED, VERDICT_DENIED)
        )

        if is_new_decision:
            if verdict == VERDICT_GRANTED:
                state["gate"]["granted"] += 1
            else:
                state["gate"]["denied"] += 1

        state["results"].append({"timestamp": timestamp, "detections": detections})
        state["results"] = state["results"][-MAX_RESULTS_PER_USER:]

        if is_new_decision:
            violation_count = sum(
                1 for d in detections if d.get("detected") and d.get("type", "").startswith("NO-")
            )
            record = DetectionRecord(
                user_id=int(user_id),
                detections_json=json.dumps(detections),
                violation_count=violation_count,
                verdict=verdict,
                missing_ppe=",".join(missing),
            )
            db.session.add(record)
            db.session.commit()

        return jsonify({
            "success": True,
            "processed": True,
            "timestamp": timestamp,
            "detections": detections,
            "verdict": verdict,
            "missing_ppe": missing,
        })

    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as JSON
        current_app.logger.exception("Error processing frame")
        return jsonify({"success": False, "message": str(exc)}), 500
