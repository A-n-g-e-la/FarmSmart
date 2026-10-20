import logging
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db_connection
from app.services.officer_registration_service import normalize_officer_email


logger = logging.getLogger(__name__)
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_hex(32), method="scrypt")


def _invalid_credentials():
    return {"ok": False, "code": "invalid_credentials", "message": "Invalid email or password."}


def _close_resources(cursor, connection):
    for label, resource in (("cursor", cursor), ("connection", connection)):
        if resource is not None:
            try:
                resource.close()
            except Exception:
                logger.warning("Officer authentication %s cleanup failed.", label)


def authenticate_officer(email, password):
    email = normalize_officer_email(email)
    if email is None or not isinstance(password, str) or not password or len(password) > 1024:
        return _invalid_credentials()
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT u.user_id, u.password_hash, u.role, u.status, "
            "o.agricultural_officer_id, o.approved_at FROM users AS u "
            "LEFT JOIN agricultural_officers AS o ON o.user_id = u.user_id "
            "WHERE u.email = %s LIMIT 1",
            (email,),
        )
        user = cursor.fetchone()
        # Unknown accounts still incur the same password verification operation.
        stored_hash = user["password_hash"] if user is not None else _DUMMY_PASSWORD_HASH
        password_matches = check_password_hash(stored_hash, password)
        if (user is None or not password_matches
                or user["role"] != "officer" or user["status"] != "active"
                or user["agricultural_officer_id"] is None or user["approved_at"] is None):
            return _invalid_credentials()
        return {"ok": True, "identity": {
            "user_id": user["user_id"], "agricultural_officer_id": user["agricultural_officer_id"],
        }}
    except Exception:
        logger.error("Officer authentication failed.")
        return {"ok": False, "code": "authentication_error",
                "message": "Authentication could not be completed. Try again later."}
    finally:
        _close_resources(cursor, connection)


def get_active_officer_identity(user_id, agricultural_officer_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        # The profile must still belong to this user and remain approved. This
        # SELECT runs on every protected request; never trust cached approval.
        cursor.execute(
            "SELECT u.user_id, o.agricultural_officer_id FROM users AS u "
            "JOIN agricultural_officers AS o ON o.user_id = u.user_id "
            "WHERE u.user_id = %s AND o.agricultural_officer_id = %s "
            "AND u.role = 'officer' AND u.status = 'active' "
            "AND o.approved_at IS NOT NULL LIMIT 1",
            (user_id, agricultural_officer_id),
        )
        row = cursor.fetchone()
        if row is None:
            return {"ok": False, "code": "invalid_session"}
        return {"ok": True, "identity": {
            "user_id": row["user_id"], "agricultural_officer_id": row["agricultural_officer_id"],
        }}
    except Exception:
        logger.error("Officer session authorization check failed.")
        return {"ok": False, "code": "authentication_error"}
    finally:
        _close_resources(cursor, connection)
