from flask import Flask
from app.config import Config
from app.db import get_db_connection

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    return app