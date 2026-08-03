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

    def to_dict(self):
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "detections": json.loads(self.detections_json),
            "violation_count": self.violation_count,
            "verdict": self.verdict,
            "missing_ppe": [p for p in self.missing_ppe.split(",") if p],
        }
