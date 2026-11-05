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
    # Only literal true enables the foundation; malformed values fail closed.
    USSD_PERSISTENT_FLOW_ENABLED = {"true": True, "false": False}.get(
        os.getenv("USSD_PERSISTENT_FLOW_ENABLED", "false").lower(), "invalid")
    USSD_RELAY_SECRET = os.getenv("USSD_RELAY_SECRET")
    USSD_TRUSTED_RELAY_IPS = os.getenv("USSD_TRUSTED_RELAY_IPS")
    AFRICASTALKING_API_KEY = os.getenv("AFRICASTALKING_API_KEY")
    USSD_PHONE_CALLBACKS_PER_MINUTE = os.getenv("USSD_PHONE_CALLBACKS_PER_MINUTE", 20)
    USSD_PHONE_CALLBACK_BURST = os.getenv("USSD_PHONE_CALLBACK_BURST", 5)
    USSD_NEW_SESSIONS_PER_MINUTE = os.getenv("USSD_NEW_SESSIONS_PER_MINUTE", 3)
    USSD_GLOBAL_CALLBACKS_PER_SECOND = os.getenv("USSD_GLOBAL_CALLBACKS_PER_SECOND", 10)
    USSD_GLOBAL_CALLBACK_BURST = os.getenv("USSD_GLOBAL_CALLBACK_BURST", 10)
    USSD_SESSION_SECONDS = os.getenv("USSD_SESSION_SECONDS", 180)
    USSD_RETENTION_SECONDS = os.getenv("USSD_RETENTION_SECONDS", 86400)
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
