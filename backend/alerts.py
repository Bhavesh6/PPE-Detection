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

import math
import threading
from datetime import datetime, timezone

import site_settings
from extensions import db
from models import SensorAlert, SensorReading, SensorReadingLog

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
    """Record a new alert. Returns (alert_dict, error).

    At most one active alert per kind: a sensor re-reporting the same
    ongoing hazard every few seconds (or an operator re-firing the same
    test) would otherwise pile up duplicate active rows for one real
    event. Whoever clears the existing one clears it for good — the next
    report after that starts a fresh alert, same as the first.
    """
    if severity not in SEVERITIES:
        return None, f"severity must be one of {', '.join(SEVERITIES)}"
    kind = str(kind or "").strip()
    if not kind:
        return None, "kind is required"
    kind = kind[:40]

    existing = (
        SensorAlert.query
        .filter(SensorAlert.kind == kind, SensorAlert.acknowledged_at.is_(None))
        .order_by(SensorAlert.timestamp.desc())
        .first()
    )
    if existing:
        return existing.to_dict(), None

    alert = SensorAlert(
        kind=kind,
        severity=severity,
        message=str(message or "").strip()[:255],
        source=(str(source).strip()[:80] or None) if source else None,
    )
    db.session.add(alert)
    db.session.commit()
    invalidate()
    return alert.to_dict(), None


def evaluate_reading(kind, value):
    """Classify a raw sensor value against its configured threshold.

    Returns (severity, threshold_cfg). severity is None if no threshold is
    configured for this kind yet, or the value doesn't cross either level —
    in both cases the reading is still worth logging, just not alerting on.
    """
    cfg = (site_settings.get("sensor_thresholds") or {}).get(kind)
    if not cfg:
        return None, None

    direction = cfg.get("direction", "above")

    def crosses(level):
        if level is None:
            return False
        return value >= level if direction == "above" else value <= level

    if crosses(cfg.get("critical_at")):
        return "critical", cfg
    if crosses(cfg.get("warning_at")):
        return "warning", cfg
    return None, cfg


# auto mode alone posts every ~4s per kind, forever — with no cap the log
# grows without bound for as long as a demo (or a real deployment) stays
# up. This is plenty for a trend chart or a short history list (both cap
# well under this), so the oldest rows past it are just dead weight.
MAX_LOG_ROWS_PER_KIND = 2000


def _prune_reading_log(kind):
    stale_ids = (
        db.session.query(SensorReadingLog.id)
        .filter(SensorReadingLog.kind == kind)
        .order_by(SensorReadingLog.timestamp.desc())
        .offset(MAX_LOG_ROWS_PER_KIND)
        .subquery()
    )
    SensorReadingLog.query.filter(SensorReadingLog.id.in_(db.session.query(stale_ids.c.id))).delete(
        synchronize_session=False
    )


def _save_reading(kind, value, unit, source=None):
    row = db.session.get(SensorReading, kind)
    if row is None:
        row = SensorReading(kind=kind, value=value, unit=unit)
        db.session.add(row)
    else:
        row.value = value
        row.unit = unit
    # Appended alongside the overwrite above, not instead of it — the
    # snapshot answers "what's it reading right now", this answers
    # "what has it read" (see SensorReadingLog's docstring).
    db.session.add(SensorReadingLog(kind=kind, value=value, unit=unit, source=source))
    _prune_reading_log(kind)
    db.session.commit()


def report_reading(kind, value, unit=None, source=None):
    """A device reporting a raw sensor value (e.g. gas ppm).

    Always stores the latest value for the console's live readout, and
    logs it to the full history. If it crosses a configured threshold,
    raises the exact same alert report() does — a threshold breach IS an
    alert, just triggered by a number instead of a device deciding the
    severity itself. Returns ({kind, value, severity, alert}, error).
    """
    kind = str(kind or "").strip()
    if not kind:
        return None, "kind is required"
    kind = kind[:40]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None, "value must be a number"
    # NaN/Infinity survive float() but not JSON: Python serializes them as
    # the bare tokens NaN/Infinity, which every browser's JSON.parse
    # rejects. One such reading is stored, then returned forever by
    # /api/alerts/readings — breaking the live readout for every client
    # until the row is deleted by hand. Reject it at the door instead.
    if not math.isfinite(value):
        return None, "value must be a finite number"

    severity, cfg = evaluate_reading(kind, value)
    # Bounded to the column width — SQLite ignores the declared length and
    # stores an oversized string happily, but Postgres (what DATABASE_URL
    # points at in production) raises DataError and 500s the request.
    unit = str(unit).strip()[:20] if unit else (cfg or {}).get("unit")
    _save_reading(kind, value, unit, source)

    alert = None
    if severity:
        display_unit = (cfg or {}).get("unit") or unit or ""
        message = f"{value:g}{display_unit} crossed the {severity} threshold"
        alert, error = report(kind, severity, message=message, source=source)
        if error:
            return None, error

    return {"kind": kind, "value": value, "severity": severity, "alert": alert}, None


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
