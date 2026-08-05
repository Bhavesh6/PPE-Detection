import json
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db


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
            "created_at": self.created_at.isoformat() if self.created_at else None,
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
            "timestamp": self.timestamp.isoformat(),
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
            "timestamp": self.timestamp.isoformat(),
            "detections": json.loads(self.detections_json),
            "violation_count": self.violation_count,
            "verdict": self.verdict,
            "missing_ppe": [p for p in self.missing_ppe.split(",") if p],
            "policy": json.loads(self.policy_json) if self.policy_json else None,
            "has_evidence": bool(self.evidence_file),
        }
