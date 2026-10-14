import logging
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db_connection
from app.services.registration_service import normalize_kenyan_phone


logger = logging.getLogger(__name__)

# Unknown users still incur a password check, reducing account-existence timing clues.
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_hex(32), method="scrypt")


def _invalid_credentials():
    # Keep every ordinary authentication failure indistinguishable to the caller.
    return {
        "ok": False,
        "code": "invalid_credentials",
        "message": "Invalid phone number or password.",
    }


def authenticate_farmer(phone_number, password):
    phone = normalize_kenyan_phone(phone_number)
    # Preserve password whitespace; reject malformed or oversized inputs before SQL.
    if phone is None or not isinstance(password, str) or not password or len(password) > 1024:
        return _invalid_credentials()

    connection = None
    cursor = None
    stage = "connection"
    try:
        connection = get_db_connection()
        stage = "lookup"
        cursor = connection.cursor(dictionary=True)
        # A LEFT JOIN distinguishes missing profiles internally without exposing them.
        cursor.execute(
            "SELECT u.user_id, u.password_hash, u.role, u.status, f.farmer_id "
            "FROM users AS u LEFT JOIN farmers AS f ON f.user_id = u.user_id "
            "WHERE u.phone_number = %s LIMIT 1",
            (phone,),
        )
        user = cursor.fetchone()
        stage = "password verification"
        stored_hash = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
        password_matches = check_password_hash(stored_hash, password)
        if (
            user is None or not password_matches
            or user["role"] != "farmer" or user["status"] != "active"
            or user["farmer_id"] is None
        ):
            return _invalid_credentials()

        # Explicitly allowlist the identity; never return the fetched row or its hash.
        return {
            "ok": True,
            "identity": {"user_id": user["user_id"], "farmer_id": user["farmer_id"]},
        }
    except Exception:
        # Operational failures are separate from bad credentials; omit exception details.
        logger.error("Farmer authentication failed at %s.", stage)
        return {
            "ok": False,
            "code": "authentication_error",
            "message": "Authentication could not be completed. Try again later.",
        }
    finally:
        # This operation performs SELECT only; closing also ends its read transaction.
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer authentication %s cleanup failed.", label)


def get_active_farmer_identity(user_id, farmer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        # Recheck authorization on every protected request, without fetching a hash.
        cursor.execute(
            "SELECT u.user_id, f.farmer_id FROM users AS u "
            "JOIN farmers AS f ON f.user_id = u.user_id "
            "WHERE u.user_id = %s AND f.farmer_id = %s "
            "AND u.role = 'farmer' AND u.status = 'active' LIMIT 1",
            (user_id, farmer_id),
        )
        row = cursor.fetchone()
        if row is None:
            return {"ok": False, "code": "invalid_session"}
        return {"ok": True, "identity": {
            "user_id": row["user_id"], "farmer_id": row["farmer_id"],
        }}
    except Exception:
        logger.error("Farmer session authorization check failed.")
        return {"ok": False, "code": "authentication_error"}
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer session %s cleanup failed.", label)
