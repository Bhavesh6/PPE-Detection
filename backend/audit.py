"""Recording who changed what.

Deliberately never raises. An audit write failing must not take down the
action it was describing — a policy change that half-succeeds because
logging broke is worse than a policy change with no log line.
"""

import json

from flask import current_app
from flask_jwt_extended import get_jwt_identity

from extensions import db
from models import AuditEvent, User

# Actions worth recording: things that change what the system will do, or
# who it will do it for. Ordinary reads and gate decisions are not here —
# decisions already have their own permanent record, and logging every
# page view would bury the handful of entries that matter.
POLICY_CHANGED = "policy.changed"
USER_DELETED = "user.deleted"
WORKER_UPDATED = "worker.updated"


def record(action, summary, detail=None, actor=None):
    """Append one audit entry. Returns True if it was written."""
    try:
        if actor is None:
            identity = get_jwt_identity()
            actor = db.session.get(User, int(identity)) if identity else None

        event = AuditEvent(
            actor_id=actor.id if actor else None,
            actor_name=actor.name if actor else "Unknown",
            action=action,
            summary=summary[:255],
            detail_json=json.dumps(detail) if detail is not None else None,
        )
        db.session.add(event)
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001
        db.session.rollback()
        current_app.logger.exception("Could not write audit entry for %s", action)
        return False


def describe_policy_change(before, after):
    """Summarise a settings change in the terms an administrator used.

    Only mentions what actually moved. A diff that lists unchanged values
    makes the entries that matter harder to spot.
    """
    parts = []

    old_ppe = before.get("required_ppe") or []
    new_ppe = after.get("required_ppe") or []
    if old_ppe != new_ppe:
        added = [i for i in new_ppe if i not in old_ppe]
        removed = [i for i in old_ppe if i not in new_ppe]
        if added:
            parts.append("required " + ", ".join(added))
        if removed:
            # The direction that weakens the gate, called out plainly.
            parts.append("stopped requiring " + ", ".join(removed))

    old_conf = before.get("confidence_threshold")
    new_conf = after.get("confidence_threshold")
    if old_conf != new_conf:
        parts.append(f"confidence {old_conf} to {new_conf}")

    return "; ".join(parts) if parts else "no effective change"
