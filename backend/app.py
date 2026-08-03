import os

from dotenv import load_dotenv
from flask import Flask, jsonify
from flask_cors import CORS

load_dotenv()

from config import Config
from extensions import db, jwt


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    jwt.init_app(app)
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}}, supports_credentials=True)

    from admin import admin_bp
    from auth import auth_bp
    from detection import detection_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(detection_bp)
    app.register_blueprint(admin_bp)

    with app.app_context():
        db.create_all()

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

    return app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
