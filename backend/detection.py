import base64
import json
import threading
import time
from datetime import datetime, timezone

import cv2
import numpy as np
from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

import alerts
import audit
import evidence
import site_settings
from extensions import db
from models import DetectionRecord, User
from ppe_detection import load_model, process_frame

detection_bp = Blueprint("detection", __name__, url_prefix="/api")

_model = None
_model_lock = threading.Lock()

# Per-user detection state, keyed by user id, so concurrent users/browser
# tabs no longer stomp on one shared set of globals.
_user_state = {}
_state_lock = threading.Lock()

MAX_RESULTS_PER_USER = 50

VERDICT_GRANTED = "granted"
VERDICT_DENIED = "denied"
VERDICT_NO_PERSON = "no_person"
# An operator-captured frame, not a gate ruling. Kept out of granted/denied
# so it never moves a compliance rate — a snapshot is an observation, and
# counting it as a decision would let anyone inflate the numbers.
VERDICT_SNAPSHOT = "snapshot"
# The gate is paused site-wide by an unresolved critical sensor alert — not
# a ruling on this person's PPE, so it's kept out of granted/denied/missing
# too. Nobody was refused for what they were wearing; nobody was checked at
# all. The alert itself (backend/models.py SensorAlert) is the log entry
# that matters here, not a per-frame DetectionRecord.
VERDICT_ALERT_HOLD = "alert_hold"

# Frames of inference timing kept per session — enough to smooth out a
# single slow frame, short enough to still show a real slowdown.
INFERENCE_WINDOW = 30


def evaluate_access(detections):
    """Decide whether the gate should open for the person in this frame.

    Returns (verdict, missing_ppe_list). A frame with nobody in it is
    "no_person" rather than a denial, so an empty checkpoint doesn't log
    a stream of false violations.
    """
    present = {d["type"] for d in detections if d.get("detected")}

    if "Person" not in present:
        return VERDICT_NO_PERSON, []

    # A hazard on site overrides PPE compliance entirely — someone in full
    # gear is still not safe to admit into a gas leak. Checked before the
    # PPE comparison so a clean pass can't produce a GRANTED underneath it.
    if alerts.active_critical():
        return VERDICT_ALERT_HOLD, []

    # Read per-call rather than at import: an administrator can change what
    # the gate demands while it's running, and the next frame should honour it.
    required = site_settings.get("required_ppe")
    missing = [
        item for item in required
        if f"NO-{item}" in present or item not in present
    ]
    verdict = VERDICT_DENIED if missing else VERDICT_GRANTED
    return verdict, missing


def _record_inference(state, elapsed_ms):
    """Track a rolling window of inference times.

    A window rather than a running average: an average over a long session
    hides the slowdown you actually care about, because thousands of good
    frames drown out the recent bad ones.

    The first inference of a session is discarded. It runs one-off setup —
    memory arenas, kernel selection — and measures far slower than anything
    after it: on this machine ~11,800ms against a steady ~170ms. Averaged
    in, it would report a fraction of a frame per second for the first
    half-minute of every session and libel a model that's running fine.
    """
    if not state["inference_warmed"]:
        state["inference_warmed"] = True
        return

    samples = state["inference_ms"]
    samples.append(elapsed_ms)
    if len(samples) > INFERENCE_WINDOW:
        del samples[:-INFERENCE_WINDOW]


def _inference_stats(state):
    samples = state["inference_ms"]
    if not samples:
        return {"avg_ms": None, "last_ms": None, "fps": None, "samples": 0}
    avg = sum(samples) / len(samples)
    return {
        "avg_ms": round(avg, 1),
        "last_ms": round(samples[-1], 1),
        # What the model could sustain if nothing else were in the way —
        # not the rate the browser is actually sending frames at.
        "fps": round(1000.0 / avg, 1) if avg > 0 else None,
        "samples": len(samples),
    }


def _annotate(frame, detections, required):
    """Burn the boxes into the stored frame.

    A bare photograph shows a person; a photograph with the model's own
    boxes on it shows the reasoning. Drawn here rather than relying on the
    browser overlay so the evidence stands on its own — it has to make
    sense to someone opening the file months later with no app around it.

    Only policy-relevant classes are drawn, matching what the operator saw
    live: boxing gear the site never required would misrepresent the
    grounds for refusal.
    """
    if frame is None:
        return None

    canvas = frame.copy()
    for det in detections:
        box = det.get("box")
        label = det.get("type", "")
        if not box:
            continue
        item = label[3:] if label.startswith("NO-") else label
        if label != "Person" and required and item not in required:
            continue

        x1, y1, x2, y2 = box
        colour = (0, 0, 220) if label.startswith("NO-") else (
            (40, 200, 40) if label != "Person" else (200, 160, 40)
        )
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(canvas, f"{label} {det.get('confidence', 0):.0%}",
                    (x1, max(y1 - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    colour, 2, cv2.LINE_AA)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cv2.putText(canvas, stamp, (10, canvas.shape[0] - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2, cv2.LINE_AA)
    return canvas


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
        # Most recent frame and its detections, held so a snapshot can be
        # taken without waiting for the next one. One decoded frame per
        # active session (~1MB); released on stop().
        "last_frame": None,
        "last_detections": [],
        "inference_ms": [],
        "inference_warmed": False,
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
    # Don't hold a decoded frame — and someone's image — after the
    # checkpoint stops.
    state["last_frame"] = None
    state["last_detections"] = []
    # Timings belong to the run that produced them; carrying them into the
    # next session would report a speed that machine isn't achieving now.
    state["inference_ms"] = []
    state["inference_warmed"] = False
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
        "required_ppe": list(site_settings.get("required_ppe")),
        "inference": _inference_stats(state),
        "active_alert": alerts.active_critical(),
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
    # Filtering has to happen here, not client-side on one already-paginated
    # page — otherwise "Page 1 of N" is computed from the unfiltered total
    # while the visible rows are a filtered subset of just page 1.
    verdict = request.args.get("verdict")

    query = DetectionRecord.query.filter_by(user_id=user_id)
    if verdict in ("granted", "denied", "no_person"):
        query = query.filter_by(verdict=verdict)
    query = query.order_by(DetectionRecord.timestamp.desc())

    total = query.count()
    records = query.offset((page - 1) * per_page).limit(per_page).all()

    return jsonify({
        "success": True,
        "records": [r.to_dict() for r in records],
        "page": page,
        "per_page": per_page,
        "total": total,
    })


@detection_bp.route("/alerts/active", methods=["GET"])
@jwt_required()
def active_alerts():
    """Currently unresolved alerts, for any signed-in surface to poll.

    Not admin-gated — an operator at the gate, or another admin tab open
    elsewhere, both need to see this to know why the gate stopped, or to
    clear it. Includes warnings too, not just the critical alert that
    actually holds the gate, so a lower-severity reading still surfaces
    as a heads-up.

    Bundles today's on-site headcount too — during a real alert, "how many
    people are in there" is exactly what the popup showing it needs to say
    in the same breath, not a second fetch away. It's "granted entry
    today," not a live in/out count: there's no exit scan in this system,
    so someone who already left is still counted. Worth having anyway —
    an upper bound beats no number at all — but not to be read as exact.
    """
    from datetime import time as _time
    from models import AttendanceRecord, SensorAlert

    rows = (
        SensorAlert.query.filter(SensorAlert.acknowledged_at.is_(None))
        .order_by(SensorAlert.timestamp.desc())
        .all()
    )

    today_start = datetime.combine(datetime.now(timezone.utc).date(), _time.min)
    granted_today = (
        AttendanceRecord.query
        .filter(AttendanceRecord.timestamp >= today_start, AttendanceRecord.granted.is_(True))
        .with_entities(AttendanceRecord.user_id)
        .distinct()
        .count()
    )

    return jsonify({
        "success": True,
        "alerts": [r.to_dict() for r in rows],
        "present_count": granted_today,
    })


@detection_bp.route("/alerts/readings", methods=["GET"])
@jwt_required()
def sensor_readings():
    """The latest value reported for every sensor kind, whether or not it's
    currently breaching a threshold — a small live readout, not just the
    alert list. Not admin-gated for the same reason /alerts/active isn't.
    """
    from models import SensorReading

    rows = SensorReading.query.order_by(SensorReading.kind).all()
    return jsonify({"success": True, "readings": [r.to_dict() for r in rows]})


@detection_bp.route("/alerts/<int:alert_id>/acknowledge", methods=["POST"])
@jwt_required()
def acknowledge_alert(alert_id):
    """Clear an alert.

    Deliberately open to any signed-in user, not admin-only — this is a
    safety action, not a policy edit, and a gas alarm shouldn't wait on an
    admin being reachable. Who cleared it is still recorded, in the alert
    itself and in the audit log, exactly like an admin's policy change.
    """
    from models import SensorAlert

    identity = get_jwt_identity()
    actor = db.session.get(User, int(identity)) if identity else None
    actor_name = actor.name if actor else "Unknown"

    # Checked before the write: alerts.acknowledge() is intentionally
    # idempotent (a second operator clearing the same alarm shouldn't get an
    # error), but that means a second call changes nothing — and an audit
    # log entry for a no-op would misrepresent it as a second, separate
    # clearing of the same alert.
    existing = db.session.get(SensorAlert, alert_id)
    was_active = bool(existing and existing.acknowledged_at is None)

    alert, error = alerts.acknowledge(alert_id, actor_name)
    if error:
        return jsonify({"success": False, "message": error}), 404

    if was_active:
        audit.record(
            audit.ALERT_ACKNOWLEDGED,
            f"cleared {alert['severity']} {alert['kind']} alert" + (f" ({alert['message']})" if alert['message'] else ""),
            detail={"alert": alert},
            actor=actor,
        )
    return jsonify({"success": True, "alert": alert})


@detection_bp.route("/snapshot", methods=["POST"])
@jwt_required()
def snapshot():
    """Capture the current frame on demand.

    The gate photographs refusals by itself, but an operator sometimes needs
    a record of something the policy doesn't cover — an unsafe act, damaged
    gear, someone who talked their way through. Stored as a record with its
    own verdict so it lands in the same reviewable log rather than a
    separate pile of loose images.
    """
    user_id = get_jwt_identity()
    state = _state_for(user_id)

    frame = state.get("last_frame")
    if frame is None:
        return jsonify({"success": False, "message": "No frame available — is the checkpoint running?"}), 400

    detections = state.get("last_detections", [])
    required = site_settings.get("required_ppe")

    record = DetectionRecord(
        user_id=int(user_id),
        detections_json=json.dumps(detections),
        violation_count=sum(
            1 for d in detections if d.get("detected") and d.get("type", "").startswith("NO-")
        ),
        verdict=VERDICT_SNAPSHOT,
        missing_ppe="",
        policy_json=json.dumps({
            "required_ppe": list(required),
            "confidence_threshold": site_settings.get("confidence_threshold"),
        }),
    )
    db.session.add(record)
    db.session.commit()

    stored = evidence.save(_annotate(frame, detections, required), record.id)
    if stored:
        record.evidence_file = stored
        db.session.commit()

    return jsonify({"success": True, "record_id": record.id, "stored": bool(stored)})


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
        #
        # Timed around the model call only — decode and JSON aren't inference,
        # and lumping them in would flatter or blame the model for work it
        # didn't do. This is the number that should change when the AI HAT
        # takes over from the CPU.
        inference_started = time.perf_counter()
        _, detections = process_frame(
            frame, model, draw=False, conf=site_settings.get("confidence_threshold")
        )
        inference_ms = (time.perf_counter() - inference_started) * 1000.0
        _record_inference(state, inference_ms)

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        _update_counts(state, detections)

        state["last_frame"] = frame
        state["last_detections"] = detections

        required = site_settings.get("required_ppe")
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
                policy_json=json.dumps({
                    "required_ppe": list(required),
                    "confidence_threshold": site_settings.get("confidence_threshold"),
                }),
            )
            db.session.add(record)
            db.session.commit()

            # Keep the frame behind a refusal so the log can be reviewed —
            # and contested. Grants aren't photographed. Written after the
            # commit so the record id can name the file, and never allowed
            # to fail the decision.
            if verdict == VERDICT_DENIED:
                stored = evidence.save(_annotate(frame, detections, required), record.id)
                if stored:
                    record.evidence_file = stored
                    db.session.commit()

        return jsonify({
            "success": True,
            "processed": True,
            "timestamp": timestamp,
            "detections": detections,
            "verdict": verdict,
            "missing_ppe": missing,
            # The gate device renders its requirement list from this, so a
            # policy change reaches the panel without restarting it.
            "required_ppe": list(required),
            "inference": _inference_stats(state),
            "active_alert": alerts.active_critical(),
        })

    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as JSON
        current_app.logger.exception("Error processing frame")
        return jsonify({"success": False, "message": str(exc)}), 500
