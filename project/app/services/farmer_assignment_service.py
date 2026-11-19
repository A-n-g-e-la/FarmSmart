"""Assign existing farmers from the local CLI; never reassign or unassign."""

import logging

from app.db import get_db_connection
from app.services.officer_registration_service import normalize_officer_email
from app.services.registration_service import normalize_kenyan_phone


logger = logging.getLogger(__name__)


def _failure(code, message):
    return {"ok": False, "changed": False, "code": code, "message": message}


def _assign_locked(cursor, phone, email):
    # Every assignment takes locks in this order: farmer user/profile, then
    # officer user/profile. Approval also locks the officer user before profile.
    cursor.execute(
        "SELECT user_id, role, status FROM users WHERE phone_number = %s FOR UPDATE",
        (phone,),
    )
    farmer_user = cursor.fetchone()
    if farmer_user is None:
        return _failure("farmer_not_found", "No farmer account exists for that phone number.")
    if farmer_user["role"] != "farmer" or farmer_user["status"] != "active":
        return _failure("invalid_farmer", "The farmer account must have role farmer and be active.")
    cursor.execute(
        "SELECT farmer_id, user_id, assigned_officer_id FROM farmers WHERE user_id = %s FOR UPDATE",
        (farmer_user["user_id"],),
    )
    farmer = cursor.fetchone()
    if (farmer is None or farmer["user_id"] != farmer_user["user_id"]
            or type(farmer["farmer_id"]) is not int or farmer["farmer_id"] <= 0):
        return _failure("inconsistent_farmer", "Farmer profile is missing or inconsistent. Manual review is required.")
    cursor.execute(
        "SELECT user_id, role, status FROM users WHERE email = %s FOR UPDATE",
        (email,),
    )
    officer_user = cursor.fetchone()
    if officer_user is None:
        return _failure("officer_not_found", "No officer account exists for that email address.")
    if officer_user["role"] != "officer" or officer_user["status"] != "active":
        return _failure("invalid_officer", "The officer account must have role officer and be active.")
    cursor.execute(
        "SELECT agricultural_officer_id, user_id, approved_at "
        "FROM agricultural_officers WHERE user_id = %s FOR UPDATE",
        (officer_user["user_id"],),
    )
    officer = cursor.fetchone()
    if (officer is None or officer["user_id"] != officer_user["user_id"]
            or officer["approved_at"] is None
            or type(officer["agricultural_officer_id"]) is not int or officer["agricultural_officer_id"] <= 0):
        return _failure("invalid_officer_profile", "Officer profile is missing, inconsistent, or unapproved. Manual review is required.")
    officer_id = officer["agricultural_officer_id"]
    if farmer["assigned_officer_id"] == officer_id:
        return {"ok": True, "changed": False, "code": "already_assigned",
                "message": "Farmer is already assigned to this officer; no change is necessary."}
    if farmer["assigned_officer_id"] is not None:
        return _failure("assignment_conflict", "Farmer is assigned to another officer. Reassignment is not permitted.")
    cursor.execute(
        "UPDATE farmers SET assigned_officer_id = %s "
        "WHERE farmer_id = %s AND user_id = %s AND assigned_officer_id IS NULL",
        (officer_id, farmer["farmer_id"], farmer_user["user_id"]),
    )
    if cursor.rowcount != 1:
        return _failure("concurrent_change", "Farmer state changed; assignment was cancelled. Review before retrying.")
    return {"ok": True, "changed": True, "code": "assigned", "message": "Farmer assigned successfully."}


def assign_farmer(farmer_phone, officer_email):
    phone = normalize_kenyan_phone(farmer_phone)
    email = normalize_officer_email(officer_email)
    if phone is None or email is None:
        return _failure("invalid_input", "Provide a valid farmer phone number and officer email address.")
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor(dictionary=True)
        result = _assign_locked(cursor, phone, email)
        if result["changed"]:
            connection.commit()
        else:
            connection.rollback()
        return result
    except Exception:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                logger.error("Farmer assignment rollback failed.")
        logger.error("Farmer assignment failed.")
        return _failure("database_error", "Assignment could not be confirmed. Check account state before retrying.")
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Farmer assignment %s cleanup failed.", label)
