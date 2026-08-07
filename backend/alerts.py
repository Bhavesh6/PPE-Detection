"""Sensor-reported site hazards (gas, smoke, ...) and their effect on the gate.

A critical, unacknowledged alert holds the gate — evaluate_access() checks
this on every frame, so the lookup is cached the same way site_settings
caches the checkpoint policy: a database round-trip per frame would cost
more than the inference itself, and alerts are rare compared to frames.

The cache holds a plain dict, not the SQLAlchemy row — a live ORM instance
held past the request that loaded it can raise on attribute access once
its session is gone, so it's serialized once with to_dict() and the row
itself is left behind.
"""

import threading
from datetime import datetime, timezone

from extensions import db
from models import SensorAlert

SEVERITIES = ("warning", "critical")

_cache = {"loaded": False, "active": None}
_lock = threading.Lock()


def invalidate():
    with _lock:
        _cache["loaded"] = False


def _load():
    row = (
        SensorAlert.query
        .filter(SensorAlert.severity == "critical", SensorAlert.acknowledged_at.is_(None))
        .order_by(SensorAlert.timestamp.desc())
        .first()
    )
    _cache["active"] = row.to_dict() if row else None
    _cache["loaded"] = True


def active_critical():
    """The most recent unacknowledged critical alert, as a dict, or None."""
    with _lock:
        if not _cache["loaded"]:
            _load()
        return _cache["active"]


def report(kind, severity, message="", source=None):
    """Record a new alert. Returns (alert_dict, error)."""
    if severity not in SEVERITIES:
        return None, f"severity must be one of {', '.join(SEVERITIES)}"
    kind = str(kind or "").strip()
    if not kind:
        return None, "kind is required"

    alert = SensorAlert(
        kind=kind[:40],
        severity=severity,
        message=str(message or "").strip()[:255],
        source=(str(source).strip()[:80] or None) if source else None,
    )
    db.session.add(alert)
    db.session.commit()
    invalidate()
    return alert.to_dict(), None


def acknowledge(alert_id, actor_name):
    """Clear an alert. Idempotent — acknowledging an already-cleared alert
    is not an error, since two operators racing to clear the same gas
    alarm shouldn't produce a confusing failure for whoever's second.
    Returns (alert_dict, error).
    """
    alert = db.session.get(SensorAlert, alert_id)
    if alert is None:
        return None, "Alert not found"
    if alert.acknowledged_at is None:
        alert.acknowledged_at = datetime.now(timezone.utc)
        alert.acknowledged_by = actor_name
        db.session.commit()
        invalidate()
    return alert.to_dict(), None
