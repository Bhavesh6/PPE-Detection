import os
from datetime import timedelta


def _database_uri():
    """Resolve the database URL, normalising managed-Postgres quirks.

    Render/Heroku hand out URLs beginning with "postgres://", a scheme
    SQLAlchemy 1.4+ dropped in favour of "postgresql://". Left as-is it
    fails at startup with "Can't load plugin: sqlalchemy.dialects:postgres".

    Falls back to local SQLite for development. Note that SQLite on an
    ephemeral container filesystem (Render, HF Spaces) is wiped on every
    restart — set DATABASE_URL to a managed Postgres instance in production
    or the compliance record will not survive a redeploy.
    """
    url = os.environ.get("DATABASE_URL", "sqlite:///app.db")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")

    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Managed Postgres drops idle connections; pre-ping avoids handing the
    # app a dead one, and recycling keeps connections under that timeout.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"pool_pre_ping": True, "pool_recycle": 280}
        if SQLALCHEMY_DATABASE_URI.startswith("postgresql")
        else {}
    )

    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-jwt-secret-change-me")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(days=7)

    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

    # Where refusal evidence frames are written. Kept on disk rather than in
    # the database: these are ~50KB JPEGs written on every refusal, and
    # storing them as BLOBs bloats the backups that exist to protect the
    # decision record itself. Same ephemeral-filesystem caveat as SQLite —
    # point EVIDENCE_DIR at a mounted volume in production or the images
    # vanish on redeploy while the records that reference them survive.
    EVIDENCE_DIR = os.environ.get(
        "EVIDENCE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "evidence"),
    )

    # Refusal images are personal data — someone's face, tied to a name and a
    # timestamp. They age out on a timer so the system isn't quietly building
    # a permanent photographic record of every worker's bad day. The decision
    # itself is kept; only the image expires.
    EVIDENCE_RETENTION_DAYS = int(os.environ.get("EVIDENCE_RETENTION_DAYS", "30"))

    # Comma-separated list of allowed frontend origins for CORS.
    CORS_ORIGINS = [
        origin.strip()
        for origin in os.environ.get("CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
        if origin.strip()
    ]
