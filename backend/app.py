import os

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

load_dotenv()

from config import Config, check_secrets
from extensions import db, jwt, limiter

# At import, not inside __main__: production runs under gunicorn, which
# imports this module and never executes that block — so a check placed
# there would pass silently in the one environment it exists to protect.
check_secrets()


def _add_missing_columns():
    """Add columns introduced after a database was first created.

    db.create_all() builds missing *tables* but never alters existing ones,
    so a deployment that predates a new column starts up fine and then
    fails on first query. There's no migration tool here, and adding one
    for a handful of nullable columns would be heavier than the problem.

    Only ever adds nullable columns — nothing here rewrites or drops data.
    """
    from sqlalchemy import inspect, text

    additions = {
        "detection_records": {
            "policy_json": "TEXT",
            "evidence_file": "VARCHAR(120)",
        },
    }

    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())

    for table, columns in additions.items():
        if table not in existing_tables:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name in present:
                continue
            try:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                db.session.commit()
            except Exception:
                db.session.rollback()
                app_logger = __import__("logging").getLogger(__name__)
                app_logger.exception("Could not add column %s.%s", table, name)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    jwt.init_app(app)
    limiter.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}}, supports_credentials=True)

    from admin import admin_bp
    from auth import auth_bp
    from cctv import cctv_bp
    from detection import detection_bp
    from gate import gate_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(detection_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(gate_bp)
    app.register_blueprint(cctv_bp)

    with app.app_context():
        db.create_all()
        _add_missing_columns()

    @app.route("/")
    def index():
        return jsonify({"status": "ok", "service": "PPE Detection API"})

    @app.route("/api/health")
    def health():
        """Unauthenticated liveness check.

        Lives under /api/ deliberately: CORS is only configured for that
        prefix, so a browser on the frontend origin can actually read this.
        The kiosk device uses it to show whether the service is up before
        anyone tries to start a checkpoint.
        """
        return jsonify({"status": "ok", "service": "PPE Detection API"})

    @jwt.unauthorized_loader
    def handle_missing_token(reason):
        return jsonify({"success": False, "message": "Authentication required"}), 401

    @jwt.invalid_token_loader
    def handle_invalid_token(reason):
        return jsonify({"success": False, "message": "Invalid or expired token"}), 401

    @app.errorhandler(429)
    def handle_rate_limit(e):
        # Matches the {success, message} shape every other error response
        # uses — Flask-Limiter's default is plain text, which would be the
        # one endpoint on this API that looks different when it fails.
        return jsonify({"success": False, "message": "Too many requests — slow down and try again shortly."}), 429

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
