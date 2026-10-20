import secrets

from flask import Flask
from app.config import Config
from app.routes.farmer import farmer_bp
from app.routes.officer import officer_bp

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    # A random process-local key supports development without writing local secrets.
    # Set SECRET_KEY in the environment for persistence across restarts/workers.
    if not app.secret_key:
        app.secret_key = secrets.token_hex(32)

    # Register farmer registration, backend login, protected home, and logout routes.
    app.register_blueprint(farmer_bp)
    app.register_blueprint(officer_bp)

    return app
