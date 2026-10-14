import logging
import re
from collections.abc import Mapping

from mysql.connector import Error, IntegrityError
from werkzeug.security import generate_password_hash

from app.db import get_db_connection
from app.models.farmer import create_farmer_profile
from app.models.user import create_farmer_user, phone_number_exists


logger = logging.getLogger(__name__)


def normalize_kenyan_phone(phone_number):
    # Accept mobile-format numbers with common visual separators, not extensions.
    if not isinstance(phone_number, str) or len(phone_number) > 40:
        return None
    if not re.fullmatch(r"[+0-9 ()\-]+", phone_number):
        return None
    compact = re.sub(r"[ ()\-]", "", phone_number)
    if compact.startswith("+254"):
        national = compact[4:]
    elif compact.startswith("00254"):
        national = compact[5:]
    elif compact.startswith("254"):
        national = compact[3:]
    elif compact.startswith("0"):
        national = compact[1:]
    else:
        national = compact
    # Kenya mobile national numbers have nine digits beginning with 1 or 7.
    # This validates format, not allocation, ownership, or whether a SIM is active.
    if not re.fullmatch(r"[17][0-9]{8}", national):
        return None
    return "+254" + national


def _failure(code, errors):
    # Never include submitted values, passwords, hashes, or database diagnostics.
    return {"ok": False, "code": code, "errors": errors}


def _rollback(connection):
    # Attempt rollback without replacing the safe error result if cleanup fails.
    if connection is not None:
        try:
            connection.rollback()
        except Exception:
            # Exception messages and tracebacks may contain SQL values or credentials.
            logger.error("Farmer registration rollback failed.")


def register_farmer(data):
    # Validate before opening a connection; passwords are deliberately not trimmed.
    if not isinstance(data, Mapping):
        return _failure("validation_error", {"request": "Provide a JSON object."})
    errors = {}
    names = {}
    for field in ("first_name", "last_name"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            errors[field] = "This field is required."
        elif len(value.strip()) > 100:
            errors[field] = "Use at most 100 characters."
        else:
            names[field] = value.strip()
    phone = normalize_kenyan_phone(data.get("phone_number"))
    if phone is None:
        errors["phone_number"] = "Provide a valid Kenyan mobile phone number."
    password = data.get("password")
    confirmation = data.get("password_confirmation")
    if not isinstance(password, str) or not password.strip():
        errors["password"] = "A password is required."
    elif len(password) < 8:
        errors["password"] = "Use at least 8 characters."
    elif len(password) > 1024:
        errors["password"] = "Use at most 1024 characters."
    if not isinstance(confirmation, str) or not confirmation:
        errors["password_confirmation"] = "Confirm your password."
    elif isinstance(password, str) and confirmation != password:
        errors["password_confirmation"] = "Passwords do not match."
    location = data.get("location")
    if location is not None:
        if not isinstance(location, str):
            errors["location"] = "Location must be text."
        elif len(location.strip()) > 150:
            errors["location"] = "Use at most 150 characters."
        else:
            location = location.strip() or None
    if errors:
        return _failure("validation_error", errors)

    connection = None
    cursor = None
    inserting_user = False
    stage = "password hashing"
    try:
        # Include hashing failures in the safe server-error path, before connecting.
        password_hash = generate_password_hash(password, method="scrypt")
        # Reuse the existing helper; manage this operation's transaction here.
        stage = "connection"
        connection = get_db_connection()
        stage = "transaction start"
        connection.start_transaction()
        stage = "cursor creation"
        cursor = connection.cursor()
        stage = "duplicate check"
        if phone_number_exists(cursor, phone):
            _rollback(connection)
            return _failure(
                "duplicate_phone",
                {"phone_number": "This phone number is already registered."},
            )
        # Both parameterized INSERTs share one connection and one transaction.
        stage = "user creation"
        inserting_user = True
        user_id = create_farmer_user(
            cursor, names["first_name"], names["last_name"], phone, password_hash
        )
        inserting_user = False
        stage = "farmer profile creation"
        farmer_id = create_farmer_profile(cursor, user_id, location)
        stage = "commit"
        connection.commit()
        return {
            "ok": True,
            "message": "Farmer registered successfully.",
            "user_id": user_id,
            "farmer_id": farmer_id,
        }
    except IntegrityError as error:
        # The unique phone index also handles a race after the duplicate precheck.
        _rollback(connection)
        # Match the complete quoted key at the message end, not text in its value.
        # Unknown formats fail closed; never log or return the exception message.
        phone_constraint = isinstance(error.msg, str) and re.search(
            r" for key (['`])(?:users\.)?uq_users_phone_number\1\Z", error.msg
        ) is not None
        if inserting_user and error.errno == 1062 and phone_constraint:
            return _failure(
                "duplicate_phone",
                {"phone_number": "This phone number is already registered."},
            )
        # Log only a fixed operation label, never the exception or submitted values.
        logger.error("Farmer registration database failure at %s.", stage)
        return _failure("database_error", {"request": "Registration could not be completed. Try again later."})
    except Error:
        # A failed INSERT or commit must not leave a user without its profile.
        _rollback(connection)
        logger.error("Farmer registration database failure at %s.", stage)
        return _failure("database_error", {"request": "Registration could not be completed. Try again later."})
    except Exception:
        # Unexpected failures are server errors, not ordinary input-validation errors.
        _rollback(connection)
        logger.error("Farmer registration unexpected failure at %s.", stage)
        return _failure("internal_error", {"request": "Registration could not be completed. Try again later."})
    finally:
        # Always release resources; cleanup diagnostics must not reach the caller.
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    # Continue to close the connection even if closing the cursor fails.
                    logger.warning("Farmer registration %s cleanup failed.", label)
