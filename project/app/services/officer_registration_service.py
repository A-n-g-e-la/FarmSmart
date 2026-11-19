import logging
import re
from collections.abc import Mapping

from mysql.connector import Error, IntegrityError
from werkzeug.security import generate_password_hash

from app.db import get_db_connection
from app.models.agricultural_officer import create_agricultural_officer_profile
from app.models.user import create_officer_user, email_exists


logger = logging.getLogger(__name__)
_FIELDS = {"first_name", "last_name", "email", "password", "password_confirmation", "region", "organization"}
_EMAIL_LOCAL = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[a-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def normalize_officer_email(value):
    """Support ASCII dot-atom mailboxes, consistently lowercased for unicode_ci."""
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email.isascii() or len(email) > 254 or email.count("@") != 1:
        return None
    local, domain = email.split("@")
    labels = domain.split(".")
    if (not 1 <= len(local) <= 64 or not _EMAIL_LOCAL.fullmatch(local)
            or len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)):
        return None
    return email


def _failure(code, errors):
    return {"ok": False, "code": code, "errors": errors}


def _duplicate_email():
    # Do not identify the existing account, its role, status, or approval state.
    return _failure("duplicate_email", {"email": "Registration cannot be completed with this email address."})


def _rollback(connection):
    if connection is not None:
        try:
            connection.rollback()
        except Exception:
            logger.error("Officer registration rollback failed.")


def register_officer(data):
    if not isinstance(data, Mapping):
        return _failure("validation_error", {"request": "Provide a JSON object."})
    if any(field not in _FIELDS for field in data):
        return _failure("validation_error", {"request": "Provide only the supported officer registration fields."})
    errors = {}
    values = {}
    for field in ("first_name", "last_name", "region"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            errors[field] = "This field is required."
        elif len(value.strip()) > 100:
            errors[field] = "Use at most 100 characters."
        else:
            values[field] = value.strip()
    email = normalize_officer_email(data.get("email"))
    if email is None:
        errors["email"] = "Provide a valid email address of at most 254 characters."
    password = data.get("password")
    confirmation = data.get("password_confirmation")
    # Match the farmer password policy exactly; preserve password whitespace.
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
    organization = data.get("organization")
    if organization is not None:
        if not isinstance(organization, str):
            errors["organization"] = "Organization must be text."
        elif len(organization.strip()) > 150:
            errors["organization"] = "Use at most 150 characters."
        else:
            organization = organization.strip() or None
    if errors:
        return _failure("validation_error", errors)

    connection = None
    cursor = None
    inserting_user = False
    stage = "password hashing"
    try:
        password_hash = generate_password_hash(password, method="scrypt")
        stage = "connection"
        connection = get_db_connection()
        stage = "transaction start"
        connection.start_transaction()
        stage = "cursor creation"
        cursor = connection.cursor()
        stage = "duplicate check"
        if email_exists(cursor, email):
            _rollback(connection)
            return _duplicate_email()
        stage = "user creation"
        inserting_user = True
        user_id = create_officer_user(cursor, values["first_name"], values["last_name"], email, password_hash)
        inserting_user = False
        stage = "officer profile creation"
        create_agricultural_officer_profile(cursor, user_id, values["region"], organization)
        stage = "commit"
        connection.commit()
        return {
            "ok": True, "status": "pending",
            "message": "Officer account created. Approval is required before you can log in. You have not been logged in.",
        }
    except IntegrityError as error:
        _rollback(connection)
        email_constraint = isinstance(error.msg, str) and re.search(
            r" for key (['`])(?:users\.)?uq_users_email\1\Z", error.msg,
        ) is not None
        if inserting_user and error.errno == 1062 and email_constraint:
            return _duplicate_email()
        logger.error("Officer registration database failure at %s.", stage)
        return _failure("database_error", {"request": "Registration could not be completed. Try again later."})
    except Error:
        _rollback(connection)
        logger.error("Officer registration database failure at %s.", stage)
        return _failure("database_error", {"request": "Registration could not be completed. Try again later."})
    except Exception:
        _rollback(connection)
        logger.error("Officer registration unexpected failure at %s.", stage)
        return _failure("internal_error", {"request": "Registration could not be completed. Try again later."})
    finally:
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("Officer registration %s cleanup failed.", label)
