import json
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


def _iso_utc(dt):
    """SQLite drops tzinfo on write, so a value stamped with
    datetime.now(timezone.utc) reads back naive. isoformat() on that naive
    value has no offset, and JS's Date() parses an offset-less date-time
    string as local time — turning a fresh UTC timestamp into one that
    looks hours old (or in the future) to any client not in UTC. Every
    value here is written as UTC (see the column defaults below), so
    marking it explicitly on the way out is correct, not a guess.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)  # null for Google-only accounts
    google_sub = db.Column(db.String(255), unique=True, nullable=True, index=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    is_guest = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Worker profile — shown on the gate display when a badge is scanned.
    employee_id = db.Column(db.String(40), nullable=True)
    rfid_tag = db.Column(db.String(64), unique=True, nullable=True, index=True)
    age = db.Column(db.Integer, nullable=True)
    role = db.Column(db.String(80), nullable=True)
    photo_url = db.Column(db.String(500), nullable=True)

    @property
    def initials(self):
        parts = (self.name or "?").split()
        return "".join(p[0].upper() for p in parts[:2]) or "?"

    def to_worker_dict(self):
        """Profile for the gate display — no credentials, no admin flags."""
        return {
            "id": self.id,
            "name": self.name,
            "initials": self.initials,
            "employee_id": self.employee_id,
            "age": self.age,
            "role": self.role,
            "photo_url": self.photo_url,
        }

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_public_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "is_admin": self.is_admin,
            "is_guest": self.is_guest,
            "initials": self.initials,
            "employee_id": self.employee_id,
            "role": self.role,
            "age": self.age,
            "photo_url": self.photo_url,
            "created_at": _iso_utc(self.created_at),
        }


class SiteSetting(db.Model):
    """Checkpoint policy an administrator can change without a redeploy.

    Values are JSON-encoded so a setting can be a list (which PPE is
    required) or a number (confidence threshold) without a column per
    setting. Reads are cached in the detection layer — /api/socket runs
    per frame, and a database round-trip there would cost more than the
    inference does.
    """

    __tablename__ = "site_settings"

    key = db.Column(db.String(60), primary_key=True)
    value_json = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    @property
    def value(self):
        return json.loads(self.value_json)


class AttendanceRecord(db.Model):
    """One row per badge scan at the gate.

    Kept separate from DetectionRecord: that logs what the camera saw, this
    logs that a named person presented themselves and whether they were let
    in. Attendance is the record a supervisor is asked for; detections are
    the evidence behind it.
    """

    __tablename__ = "attendance_records"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    granted = db.Column(db.Boolean, default=False, nullable=False)
    missing_ppe = db.Column(db.String(255), default="", nullable=False)

    user = db.relationship("User", backref="attendance")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.user.name if self.user else None,
            "timestamp": _iso_utc(self.timestamp),
            "granted": self.granted,
            "missing_ppe": [p for p in self.missing_ppe.split(",") if p],
        }


class DetectionRecord(db.Model):
    """A single permanently-logged detection frame: what PPE was/wasn't present, for whom, and when."""

    __tablename__ = "detection_records"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    detections_json = db.Column(db.Text, nullable=False)
    violation_count = db.Column(db.Integer, default=0, nullable=False)

    # Gate verdict: "granted", "denied", or "no_person" (nobody at the checkpoint).
    verdict = db.Column(db.String(20), default="no_person", nullable=False, index=True)
    # Comma-separated required PPE that was missing, e.g. "Hardhat,Safety Vest".
    missing_ppe = db.Column(db.String(255), default="", nullable=False)

    # What the site required at the moment of this decision. Without it a
    # trend spanning a policy change silently mixes different rules, and an
    # old refusal can't be explained by today's settings.
    policy_json = db.Column(db.Text, nullable=True)

    # Filename of the frame that produced a refusal, relative to
    # EVIDENCE_DIR. Null for grants (nothing to answer for) and for records
    # whose image has aged out — the decision outlives the photograph.
    evidence_file = db.Column(db.String(120), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": _iso_utc(self.timestamp),
            "detections": json.loads(self.detections_json),
            "violation_count": self.violation_count,
            "verdict": self.verdict,
            "missing_ppe": [p for p in self.missing_ppe.split(",") if p],
            "policy": json.loads(self.policy_json) if self.policy_json else None,
            "has_evidence": bool(self.evidence_file),
        }


class SensorReading(db.Model):
    """The latest raw value reported for a sensor kind — not a history, just
    "what does this sensor say right now" for a small live readout. One row
    per kind, overwritten on every report. The durable record is
    SensorAlert, created only when a reading actually crosses a threshold —
    this table exists so the console can show a value even when nothing's
    wrong, which SensorAlert alone can't do.
    """

    __tablename__ = "sensor_readings"

    kind = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(20), nullable=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "updated_at": _iso_utc(self.updated_at),
        }


class SensorReadingLog(db.Model):
    """Every value a sensor has ever reported, one row per report —
    SensorReading above only keeps the latest per kind, overwritten on
    each call, so a trend or a past value is gone the moment a newer
    reading lands unless it's kept somewhere else. This is that somewhere
    else: an append-only feed for a history list or a trend chart, the
    same relationship SensorAlert has to "what's active right now".
    """

    __tablename__ = "sensor_reading_log"

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False, index=True)
    value = db.Column(db.Float, nullable=False)
    unit = db.Column(db.String(20), nullable=True)
    source = db.Column(db.String(80), nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                          nullable=False, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "timestamp": _iso_utc(self.timestamp),
        }


class SensorAlert(db.Model):
    """A sensor-reported site hazard (gas, smoke, ...) — not a PPE decision.

    Separate from DetectionRecord (what one person was wearing) and
    AuditEvent (an admin changing a setting): this is the site itself
    reporting a hazard. A critical, unacknowledged alert also holds the
    gate (see detection.evaluate_access) until someone clears it, so this
    table doubles as the record of who cleared it and when — the same
    accountability the audit log gives policy changes.
    """

    __tablename__ = "sensor_alerts"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                          nullable=False, index=True)

    kind = db.Column(db.String(40), nullable=False)
    severity = db.Column(db.String(16), nullable=False, index=True)  # "warning" | "critical"
    message = db.Column(db.String(255), nullable=False, default="")
    source = db.Column(db.String(80), nullable=True)

    acknowledged_at = db.Column(db.DateTime, nullable=True)
    # Denormalized like AuditEvent.actor_name — who cleared a gas alert
    # should still read correctly after that account is deleted.
    acknowledged_by = db.Column(db.String(120), nullable=True)

    @property
    def active(self):
        """Unacknowledged. Critical + active is what holds the gate."""
        return self.acknowledged_at is None

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": _iso_utc(self.timestamp),
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "active": self.active,
            "acknowledged_at": _iso_utc(self.acknowledged_at),
            "acknowledged_by": self.acknowledged_by,
        }


class AuditEvent(db.Model):
    """Who changed what, and when.

    The gate's own decisions are already recorded, but the settings that
    govern those decisions were not. Someone could lower the required
    equipment to nothing, walk people through, and set it back, and no
    record anywhere would show it — which makes every compliance figure
    downstream unfalsifiable.

    Append-only by intent: there is no endpoint that edits or deletes these.
    """

    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                          nullable=False, index=True)

    # Kept alongside the id so the trail still reads correctly after the
    # account is deleted — "user 7 did this" is useless once 7 is gone.
    actor_id = db.Column(db.Integer, nullable=True, index=True)
    actor_name = db.Column(db.String(120), nullable=False, default="Unknown")

    action = db.Column(db.String(60), nullable=False, index=True)
    summary = db.Column(db.String(255), nullable=False, default="")
    detail_json = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": _iso_utc(self.timestamp),
            "actor_id": self.actor_id,
            "actor_name": self.actor_name,
            "action": self.action,
            "summary": self.summary,
            "detail": json.loads(self.detail_json) if self.detail_json else None,
        }
