import secrets

from flask import Flask
from app.config import Config
from app.routes.farmer import farmer_bp
from app.routes.officer import officer_bp
from app.routes.ussd import ussd_bp
from app.cli import approve_officer_command, assign_farmer_command
from app.services.ussd_rate_limiter import LocalUssdLimiter

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.extensions["ussd_local_limiter"] = LocalUssdLimiter()
    # A random process-local key supports development without writing local secrets.
    # Set SECRET_KEY in the environment for persistence across restarts/workers.
    if not app.secret_key:
        app.secret_key = secrets.token_hex(32)

    # Register farmer registration, backend login, protected home, and logout routes.
    app.register_blueprint(farmer_bp)
    app.register_blueprint(officer_bp)
    app.register_blueprint(ussd_bp)
    app.cli.add_command(approve_officer_command)
    app.cli.add_command(assign_farmer_command)

    return app
