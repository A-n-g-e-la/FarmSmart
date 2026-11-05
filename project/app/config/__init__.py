import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")
    # Validated only when PIN helpers are used; never generated or defaulted.
    USSD_PIN_PEPPERS = os.getenv("USSD_PIN_PEPPERS")
    USSD_PIN_CURRENT_KEY_VERSION = os.getenv("USSD_PIN_CURRENT_KEY_VERSION")
    USSD_METADATA_HMAC_KEY = os.getenv("USSD_METADATA_HMAC_KEY")
    USSD_SERVICE_CODE = os.getenv("USSD_SERVICE_CODE")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(minutes=30)
    SESSION_REFRESH_EACH_REQUEST = False
    # Enable for HTTPS hosting; local HTTP development cannot send secure cookies.
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    MYSQL_HOST = os.getenv("MYSQL_HOST")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
    MYSQL_USER = os.getenv("MYSQL_USER")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE")
