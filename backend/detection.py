import base64
import threading
from datetime import datetime, timezone

import cv2
import numpy as np
from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from ppe_detection import load_model, process_frame

detection_bp = Blueprint("detection", __name__, url_prefix="/api")

_model = None
_model_lock = threading.Lock()

# Per-user detection state, keyed by user id, so concurrent users/browser
# tabs no longer stomp on one shared set of globals.
_user_state = {}
_state_lock = threading.Lock()

MAX_RESULTS_PER_USER = 50


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
        "live": {"violations": 0, "helmets": 0, "vests": 0, "gloves": 0},
        "totals": {"violations": 0, "helmets": 0, "vests": 0, "gloves": 0},
    }


def _state_for(user_id):
    with _state_lock:
        if user_id not in _user_state:
            _user_state[user_id] = _new_state()
        return _user_state[user_id]


def _update_counts(state, detections):
    live = {"violations": 0, "helmets": 0, "vests": 0, "gloves": 0}

    for detection in detections:
        type_name = detection.get("type", "")
        if not detection.get("detected"):
            continue
        if type_name.startswith("NO-"):
            live["violations"] += 1
            state["totals"]["violations"] += 1
        elif type_name in ("Hardhat", "helmet"):
            live["helmets"] += 1
            state["totals"]["helmets"] += 1
        elif type_name in ("Safety Vest", "vest"):
            live["vests"] += 1
            state["totals"]["vests"] += 1
        elif type_name in ("Gloves", "hand gloves"):
            live["gloves"] += 1
            state["totals"]["gloves"] += 1

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
        "gloves": state["live"]["gloves"],
        "totals": state["totals"],
    })


@detection_bp.route("/results", methods=["GET"])
@jwt_required()
def get_results():
    state = _state_for(get_jwt_identity())
    return jsonify({"results": state["results"]})


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

        state["results"].append({"timestamp": timestamp, "detections": detections})
        state["results"] = state["results"][-MAX_RESULTS_PER_USER:]

        return jsonify({
            "success": True,
            "processed": True,
            "timestamp": timestamp,
            "detections": detections,
        })

    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as JSON
        current_app.logger.exception("Error processing frame")
        return jsonify({"success": False, "message": str(exc)}), 500
