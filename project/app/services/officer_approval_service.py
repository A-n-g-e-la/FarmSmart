"""Transactional approval, invoked only by the local operator CLI."""

import logging

from app.db import get_db_connection
from app.services.officer_registration_service import normalize_officer_email


logger = logging.getLogger(__name__)


def _failure(code, message):
    return {"ok": False, "code": code, "message": message}


def approve_officer(email):
    email = normalize_officer_email(email)
    if email is None:
        return _failure("invalid_email", "Provide a valid registered officer email address.")
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor(dictionary=True)
        # All approvals lock in user-then-profile order. A competing approval
        # waits here and reads the committed status rather than approving twice.
        cursor.execute(
            "SELECT user_id, role, status FROM users WHERE email = %s FOR UPDATE",
            (email,),
        )
        user = cursor.fetchone()
        result = None
        if user is None:
            result = _failure("not_found", "No account exists for that email address.")
        elif user["role"] != "officer":
            result = _failure("not_officer", "Only officer accounts can be approved.")
        else:
            cursor.execute(
                "SELECT agricultural_officer_id, user_id, approved_at "
                "FROM agricultural_officers WHERE user_id = %s FOR UPDATE",
                (user["user_id"],),
            )
            profile = cursor.fetchone()
            if profile is None or profile["user_id"] != user["user_id"]:
                result = _failure("inconsistent_account", "Officer profile is missing or inconsistent. Manual review is required.")
            elif user["status"] == "disabled":
                result = _failure("disabled", "Disabled accounts cannot be approved.")
            elif user["status"] == "active" and profile["approved_at"] is not None:
                result = _failure("already_active", "Officer account is already active; no changes made.")
            elif user["status"] != "pending" or profile["approved_at"] is not None:
                result = _failure("inconsistent_account", "Officer approval state is inconsistent. Manual review is required.")
            else:
                cursor.execute(
                    "UPDATE users SET status = 'active' "
                    "WHERE user_id = %s AND role = 'officer' AND status = 'pending'",
                    (user["user_id"],),
                )
                if cursor.rowcount != 1:
                    result = _failure("concurrent_change", "Account state changed; approval was cancelled. Review before retrying.")
                else:
                    cursor.execute(
                        "UPDATE agricultural_officers SET approved_at = UTC_TIMESTAMP() "
                        "WHERE agricultural_officer_id = %s AND user_id = %s AND approved_at IS NULL",
                        (profile["agricultural_officer_id"], user["user_id"]),
                    )
                    if cursor.rowcount != 1:
                        result = _failure("concurrent_change", "Profile state changed; approval was cancelled. Review before retrying.")
        if result is not None:
            connection.rollback()
            return result
        connection.commit()
        return {"ok": True, "message": "Officer approved successfully. Account is active."}
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                logger.error("Officer approval rollback failed.")
        # Do not log driver messages, email addresses, SQL, or credentials.
        logger.error("Officer approval failed.")
        return _failure("database_error", "Approval could not be confirmed. Check account state before retrying.")
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Officer approval %s cleanup failed.", label)
