"""Authenticated web PIN enrollment/change; no web-password or USSD-route changes."""

import re
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash

from app.models.ussd_repository import UssdConflictError, ussd_transaction
from app.services.ussd_hash_capacity import scrypt_slot
from app.services.ussd_pin_service import hash_ussd_pin


def _now():
    return datetime.now(timezone.utc)


def _failure(code="unavailable"):
    messages = {
        "validation": "Enter your web password and matching PINs of exactly four digits (0-9).",
        "password": "Your web password could not be verified.",
        "locked": "PIN management is temporarily locked. Please try again in 15 minutes.",
        "unavailable": "Your USSD PIN could not be saved. Please try again later.",
    }
    return {"ok": False, "code": code, "message": messages[code]}


def _account(store, user_id, farmer_id):
    account = store.get_farmer_web_credential(user_id)
    if (account is None or account["user_id"] != user_id or account["farmer_id"] != farmer_id
            or account["role"] != "farmer" or account["status"] != "active"
            or not isinstance(account["password_hash"], str) or not account["password_hash"]):
        raise UssdConflictError("Farmer authorization refused.")
    return account


def _web_attempts(credential, now):
    if credential is None:
        return 0
    attempts, until = credential["web_failed_attempts"], credential["web_locked_until"]
    if type(attempts) is not int or not 0 <= attempts <= 5 or (attempts == 5) != (until is not None):
        raise UssdConflictError("PIN management state refused.")
    if until is not None:
        if not isinstance(until, datetime):
            raise UssdConflictError("PIN management state refused.")
        until = until.replace(tzinfo=timezone.utc) if until.tzinfo is None else until.astimezone(timezone.utc)
        return None if until > now else 0
    return attempts


def save_farmer_ussd_pin(user_id, farmer_id, password, pin, confirmation):
    """Recheck locked account/password state before atomically replacing the PIN.

    Both expensive hashes run without DB locks and share the non-waiting gate.
    Five wrong web-password checks lock this operation for 15 minutes. Existing
    web login and USSD attempt policies are unchanged; counters are distinct.
    """
    if (type(user_id) is not int or type(farmer_id) is not int
            or not 0 < user_id <= 18446744073709551615 or not 0 < farmer_id <= 18446744073709551615):
        return _failure()
    if (not isinstance(password, str) or not password or len(password) > 1024
            or not isinstance(pin, str) or re.fullmatch(r"[0-9]{4}", pin) is None
            or not isinstance(confirmation, str) or confirmation != pin):
        return _failure("validation")
    try:
        with ussd_transaction() as store:
            account = _account(store, user_id, farmer_id)
            credential = store.get_credentials(farmer_id)
            if _web_attempts(credential, _now()) is None:
                return _failure("locked")
            password_hash = account["password_hash"]
        with scrypt_slot():
            matched = check_password_hash(password_hash, password)
        if type(matched) is not bool:
            raise UssdConflictError("Password verification unavailable.")
        encoded = hash_ussd_pin(farmer_id, pin) if matched else None
        with ussd_transaction() as store:
            account = _account(store, user_id, farmer_id)
            if account["password_hash"] != password_hash:
                raise UssdConflictError("Farmer authorization changed.")
            credential = store.get_credentials(farmer_id)
            now = _now()
            attempts = _web_attempts(credential, now)
            if attempts is None:
                return _failure("locked")
            if not matched:
                attempts += 1
                store.ensure_credentials(farmer_id, now)
                store.set_attempt_state(farmer_id, attempts,
                                        now + timedelta(minutes=15) if attempts == 5 else None, now, web=True)
                result = _failure("locked" if attempts == 5 else "password")
            else:
                store.replace_pin_hash(farmer_id, encoded["pin_hash"], encoded["pin_key_version"], now)
                result = {"ok": True}
        return result
    except Exception:
        # No credentials, request values, SQL diagnostics or exception messages.
        return _failure()
