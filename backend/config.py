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


DEV_SECRET_KEY = "dev-secret-change-me"
DEV_JWT_SECRET_KEY = "dev-jwt-secret-change-me"

# Set by the platform itself, not by us. Their presence is the most reliable
# "this is deployed, not somebody's laptop" signal available without asking
# the deployer to remember another setting — which is exactly the thing that
# gets forgotten and causes this problem in the first place.
HOST_MARKERS = (
    "RENDER",               # Render
    "SPACE_ID",             # Hugging Face Spaces
    "DYNO",                 # Heroku
    "FLY_APP_NAME",         # Fly.io
    "RAILWAY_ENVIRONMENT",  # Railway
    "K_SERVICE",            # Google Cloud Run
    "WEBSITE_INSTANCE_ID",  # Azure App Service
)


def _looks_deployed():
    return any(os.environ.get(marker) for marker in HOST_MARKERS)


def check_secrets():
    """Refuse to serve traffic with the placeholder signing keys.

    JWTs are signed with JWT_SECRET_KEY. The fallback below is a literal in
    this file, so a deployment that never set the real one signs tokens with
    a value anybody reading the repository already knows — and a forged token
    is indistinguishable from a real one, including an admin's. That is not a
    slow leak; it is account takeover from a published string.

    Local development keeps the convenient defaults and only warns, because
    the failure mode there is nobody's problem. Anything running on a known
    host raises instead: better a container that refuses to start than one
    that starts wide open and looks perfectly healthy.
    """
    weak = []
    if os.environ.get("SECRET_KEY", DEV_SECRET_KEY) == DEV_SECRET_KEY:
        weak.append("SECRET_KEY")
    if os.environ.get("JWT_SECRET_KEY", DEV_JWT_SECRET_KEY) == DEV_JWT_SECRET_KEY:
        weak.append("JWT_SECRET_KEY")
    if not weak:
        return

    names = " and ".join(weak)
    if _looks_deployed():
        raise RuntimeError(
            f"Refusing to start: {names} still set to the development default, "
            "which is published in this repository's config.py — anyone could "
            "forge an admin token. Generate one per variable with "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"` '
            "and set it in the host's environment settings."
        )
    print(
        f"WARNING: {names} using the development default. Fine locally; this "
        "will refuse to start once deployed. See .env.example.",
        flush=True,
    )


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)

    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Managed Postgres drops idle connections; pre-ping avoids handing the
    # app a dead one, and recycling keeps connections under that timeout.
    SQLALCHEMY_ENGINE_OPTIONS = (
        {"pool_pre_ping": True, "pool_recycle": 280}
        if SQLALCHEMY_DATABASE_URI.startswith("postgresql")
        else {}
    )

    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", DEV_JWT_SECRET_KEY)
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

    # Spoken gate announcements. Blank key means "not configured" — the
    # frontend falls back to the browser's own (robotic) speechSynthesis
    # rather than the gate breaking when nobody's set this up yet.
    ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
    # Default is "Rachel", one of ElevenLabs' stock premade voices — any
    # voice_id from their library or a cloned voice works here.
    ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

    # Cached synthesized audio, one file per distinct phrase. The gate
    # speaks maybe half a dozen distinct sentences ever — "Access granted."
    # fires on every clean pass — so paying ElevenLabs and waiting on the
    # network for the same sentence repeatedly would be pure waste.
    SPEECH_CACHE_DIR = os.environ.get(
        "SPEECH_CACHE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "speech"),
    )
