"""Checkpoint policy that an administrator can change at runtime.

What the gate demands is a site decision, not a code decision — a
scaffolding job and a welding bay don't require the same gear. These used
to be constants, which meant changing them was a redeploy.

Reads are cached in a module-level dict because /api/socket consults the
policy on every frame; hitting the database there would cost more than
the inference itself. The cache is invalidated on write, and writes are
rare, so it stays coherent without a TTL.
"""

import json
import threading
from datetime import datetime, timezone

from extensions import db
from models import SiteSetting

# Classes the trained model can actually emit. Anything outside this set
# can't be required, because the model would never report it either way —
# a requirement nothing can satisfy would deny everyone forever.
DETECTABLE_PPE = ("Hardhat", "Safety Vest", "Mask")

DEFAULTS = {
    "required_ppe": ["Hardhat", "Safety Vest"],
    # Below this confidence a detection is treated as noise. Raising it
    # reduces false violations but risks missing genuine ones.
    "confidence_threshold": 0.25,
    # No GNSS module reporting yet, so this starts unset. Once anything
    # writes a fix — an admin by hand, or a device over /api/gate/location —
    # `source` says which, and the console decides whether to keep polling
    # for fresher fixes from that alone.
    "site_location": {"label": "", "lat": None, "lng": None, "source": None, "updated_at": None},
    # Empty until an admin configures one — a sensor kind with no threshold
    # just has its readings logged (see alerts.report_reading), raising
    # nothing. Keyed by kind, e.g. {"gas": {"warning_at": 400,
    # "critical_at": 800, "unit": "ppm", "direction": "above"}}.
    "sensor_thresholds": {},
}

_cache = {}
_lock = threading.Lock()


def _load():
    """Fill the cache from the database, falling back to defaults."""
    values = dict(DEFAULTS)
    for row in SiteSetting.query.all():
        if row.key in DEFAULTS:
            values[row.key] = row.value
    _cache.update(values)
    return values


def get_all():
    with _lock:
        if not _cache:
            _load()
        return dict(_cache)


def get(key):
    return get_all().get(key, DEFAULTS.get(key))


def invalidate():
    with _lock:
        _cache.clear()


def update(changes):
    """Validate and persist a partial settings change.

    Returns (settings, error). Validation is strict rather than forgiving:
    a silently-ignored bad value would leave the console showing a policy
    the gate isn't actually enforcing.
    """
    clean = {}

    if "required_ppe" in changes:
        items = changes["required_ppe"]
        if not isinstance(items, list):
            return None, "required_ppe must be a list"
        unknown = [i for i in items if i not in DETECTABLE_PPE]
        if unknown:
            return None, f"Model cannot detect: {', '.join(unknown)}"
        if not items:
            return None, "At least one item must be required"
        # Keep DETECTABLE_PPE's order so the UI and the gate agree on
        # how a missing-item list reads.
        clean["required_ppe"] = [i for i in DETECTABLE_PPE if i in items]

    if "confidence_threshold" in changes:
        try:
            conf = float(changes["confidence_threshold"])
        except (TypeError, ValueError):
            return None, "confidence_threshold must be a number"
        if not 0.05 <= conf <= 0.95:
            return None, "confidence_threshold must be between 0.05 and 0.95"
        clean["confidence_threshold"] = round(conf, 2)

    if "sensor_thresholds" in changes:
        thresholds = changes["sensor_thresholds"]
        if not isinstance(thresholds, dict):
            return None, "sensor_thresholds must be an object"

        def _level(cfg, key, kind):
            raw = cfg.get(key)
            if raw in (None, ""):
                return None
            try:
                return float(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{key} for {kind} must be a number")

        clean_thresholds = {}
        try:
            for kind, cfg in thresholds.items():
                if not isinstance(cfg, dict):
                    return None, f"threshold for {kind} must be an object"
                direction = cfg.get("direction", "above")
                if direction not in ("above", "below"):
                    return None, f"direction for {kind} must be 'above' or 'below'"
                warning_at = _level(cfg, "warning_at", kind)
                critical_at = _level(cfg, "critical_at", kind)
                if warning_at is None and critical_at is None:
                    return None, f"{kind} needs at least a warning or critical level"
                clean_thresholds[str(kind).strip()[:40]] = {
                    "warning_at": warning_at,
                    "critical_at": critical_at,
                    "unit": str(cfg.get("unit") or "").strip()[:20],
                    "direction": direction,
                }
        except ValueError as exc:
            return None, str(exc)
        clean["sensor_thresholds"] = clean_thresholds

    if not clean:
        return None, "No recognised settings supplied"

    for key, value in clean.items():
        row = db.session.get(SiteSetting, key)
        if row is None:
            row = SiteSetting(key=key, value_json=json.dumps(value))
            db.session.add(row)
        else:
            row.value_json = json.dumps(value)
    db.session.commit()

    invalidate()
    return get_all(), None


def set_location(lat, lng, label=None, source="manual"):
    """Persist a location fix, stamped with who/what supplied it.

    Separate from update() because a fix isn't like the other settings: it
    can arrive routinely from a device (every N seconds, once a GNSS module
    exists) rather than only from a human editing a form, and `source` /
    `updated_at` are always stamped here rather than trusted from the
    caller — a device claiming "manual" or backdating its own fix would
    make the console's staleness check meaningless.

    label=None keeps whatever label is already saved, so a device posting
    coordinates doesn't blank out the name an admin gave the gate.
    Returns (location, error).
    """
    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None, "lat and lng must be numbers"
    if not -90 <= lat <= 90:
        return None, "lat must be between -90 and 90"
    if not -180 <= lng <= 180:
        return None, "lng must be between -180 and 180"

    current = get("site_location") or {}
    value = {
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "label": str(label).strip()[:120] if label is not None else (current.get("label") or ""),
        "source": source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    row = db.session.get(SiteSetting, "site_location")
    if row is None:
        row = SiteSetting(key="site_location", value_json=json.dumps(value))
        db.session.add(row)
    else:
        row.value_json = json.dumps(value)
    db.session.commit()

    invalidate()
    return get("site_location"), None
