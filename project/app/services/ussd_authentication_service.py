"""USSD authentication and menu transitions behind opt-in callback ingress.

One PIN submission per dial. Public results contain no account identifiers.
Stage 2C.2B must authenticate callback origin and provide ingress rate limits.
"""

import base64
import hmac
import re
from datetime import datetime, timedelta, timezone

from flask import current_app

from app.models.ussd_fingerprints import derive_session_key, fingerprint_input, fingerprint_phone
from app.models.ussd_repository import SAFE_RESPONSES, UssdPersistenceError, ussd_transaction
from app.services.registration_service import normalize_kenyan_phone
from app.services.ussd_pin_service import validate_ussd_pin_credential, verify_ussd_pin


class _Refused(UssdPersistenceError):
    """Fixed internal rejection; rollback rather than persist an auth transition."""


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    if not isinstance(value, datetime):
        raise _Refused("USSD verification is unavailable.")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _result(code="verification_failed", *, authenticated=False):
    # Duplicate recognition stays internal: exposing it could enumerate accounts.
    return {"authenticated": authenticated, "response": SAFE_RESPONSES[code]}


def _binding(phone_number, session_id, service_code):
    phone = normalize_kenyan_phone(phone_number)
    if phone is None or not isinstance(service_code, str):
        raise _Refused("USSD verification is unavailable.")
    configured = current_app.config.get("USSD_SERVICE_CODE")
    raw_key = current_app.config.get("USSD_METADATA_HMAC_KEY")
    if not isinstance(raw_key, str) or len(raw_key) != 44:
        raise _Refused("USSD verification is unavailable.")
    key = base64.b64decode(raw_key, validate=True)
    if len(key) != 32 or base64.b64encode(key).decode("ascii") != raw_key:
        raise _Refused("USSD verification is unavailable.")
    session_key = derive_session_key(key, service_code, session_id)
    if not isinstance(configured, str) or not hmac.compare_digest(service_code, configured):
        raise _Refused("USSD verification is unavailable.")
    return phone, session_key, fingerprint_phone(key, phone), key


def _candidate(row):
    if (row is None or row["role"] != "farmer" or row["status"] != "active"
            or type(row["user_id"]) is not int or row["user_id"] <= 0
            or type(row["farmer_id"]) is not int or row["farmer_id"] <= 0):
        return None
    return row


def _load(store, phone, session_key):
    # All phases lock user -> profile -> credential -> session -> callback.
    farmer = _candidate(store.get_farmer_by_phone(phone))
    credential = store.get_credentials(farmer["farmer_id"]) if farmer is not None else None
    session = store.get_session(session_key)
    return farmer, credential, session


def _check_session(session, phone_tag, service_code, now):
    if (session is None or not isinstance(session["phone_tag"], bytes)
            or not hmac.compare_digest(session["phone_tag"], phone_tag)
            or session["service_code"] != service_code
            or not _stamp(session["created_at"]) <= now < _stamp(session["expires_at"])
            or _stamp(session["retain_until"]) < _stamp(session["expires_at"])):
        raise _Refused("USSD verification is unavailable.")


def _check_credentials(credential, farmer):
    if (farmer is None or credential is None or credential["farmer_id"] != farmer["farmer_id"]
            or type(credential["pin_version"]) is not int or not 1 <= credential["pin_version"] <= 18446744073709551615
            or type(credential["failed_attempts"]) is not int or not 0 <= credential["failed_attempts"] <= 3):
        raise _Refused("USSD verification is unavailable.")
    until = credential["locked_until"]
    if (credential["failed_attempts"] == 3) != (until is not None):
        raise _Refused("USSD verification is unavailable.")
    if until is not None:
        _stamp(until)
    validate_ussd_pin_credential(credential["pin_hash"], credential["pin_key_version"])


def _check_prefix(store, session_key, position, fingerprint, response_code):
    previous = store.get_callback_result(session_key, position)
    if (previous is None or not hmac.compare_digest(previous["input_fingerprint"], fingerprint)
            or previous["response_text"] != SAFE_RESPONSES[response_code] or previous["response_status"] != 200):
        raise _Refused("USSD verification is unavailable.")


def _duplicate(store, session_key, position, fingerprint, session, credential):
    previous = store.get_callback_result(session_key, position)
    if previous is None:
        return None
    _check_session(session, session["phone_tag"], session["service_code"], _now())
    if (not hmac.compare_digest(previous["input_fingerprint"], fingerprint)
            or previous["response_status"] != 200):
        raise _Refused("USSD verification is unavailable.")
    # Cache recognition is never an authorization grant for another operation.
    if previous["response_text"] == SAFE_RESPONSES["pin_verified"]:
        if (session["state"] != "verified" or credential is None
                or session["verified_pin_version"] != credential["pin_version"]
                or not _stamp(session["created_at"]) <= _stamp(session["authenticated_at"]) <= _now()
                or (credential["locked_until"] is not None and _stamp(credential["locked_until"]) > _now())):
            raise _Refused("USSD verification is unavailable.")
        return _result("pin_verified")
    if previous["response_text"] == SAFE_RESPONSES["verification_failed"]:
        return _result()
    if previous["response_text"] == SAFE_RESPONSES["pin_prompt"]:
        if session["state"] != "pin":
            raise _Refused("USSD verification is unavailable.")
        return _result("pin_prompt")
    raise _Refused("USSD verification is unavailable.")


def identify_ussd_candidate(phone_number, session_id, service_code):
    """Accept the `1` step of an existing welcome session without exposing identity.

    Stage 2C.2B must have stored its empty callback at position 0. This method
    creates no session and does not identify a caller as authenticated.
    """
    try:
        phone, session_key, phone_tag, key = _binding(phone_number, session_id, service_code)
        current = fingerprint_input(key, session_key, phone_tag, "1")
        prefix = fingerprint_input(key, session_key, phone_tag, "")
        with ussd_transaction() as store:
            farmer, credential, session = _load(store, phone, session_key)
            now = _now()  # Read time after acquiring locks, not before waiting.
            _check_session(session, phone_tag, service_code, now)
            farmer_id = farmer["farmer_id"] if farmer is not None else None
            if session["farmer_id"] not in (None, farmer_id):
                raise _Refused("USSD verification is unavailable.")
            _check_prefix(store, session_key, 0, prefix, "welcome")
            cached = _duplicate(store, session_key, 1, current, session, credential)
            if cached is not None:
                return cached
            if session["state"] != "welcome" or session["last_input_count"] != 0:
                raise _Refused("USSD verification is unavailable.")
            store.bind_candidate(session_key, farmer_id, now)
            store.store_callback_result(session_key, 1, current, "pin_prompt", now)
            store.set_session_state(session_key, "pin", now)
        return _result("pin_prompt")
    except Exception:
        return _result()


def _verified_menu_state(farmer, credential, session, now):
    """Current authorization for a fixed menu, never a cached boolean grant."""
    _check_credentials(credential, farmer)
    if (session["state"] != "verified" or session["farmer_id"] != farmer["farmer_id"]
            or session["verified_pin_version"] != credential["pin_version"]
            or not _stamp(session["created_at"]) <= _stamp(session["authenticated_at"]) <= now
            or (credential["locked_until"] is not None and _stamp(credential["locked_until"]) > now)):
        raise _Refused("USSD verification is unavailable.")


def advance_ussd_menu(phone_number, session_id, service_code, text):
    """Cancel PIN entry or navigate the fixed success placeholder; never hash.

    `1*0` cancels; malformed numeric PIN submissions terminate without counting
    an incorrect PIN. After `1*dddd`, optional `*1` refreshes the same placeholder
    and `*0` exits. Every prefix must actually exist in the accepted ledger.
    All results, including a new verified menu response, have authenticated=False.
    """
    try:
        if (not isinstance(text, str) or len(text) > 128 or text.count("*") > 15
                or not re.fullmatch(r"1\*[0-9]+(?:\*[0-9]+)*", text)):
            return _result()
        parts = text.split("*")
        position = len(parts)
        pin_step = position == 2 and not re.fullmatch(r"[0-9]{4}", parts[1])
        verified_step = (position >= 3 and re.fullmatch(r"[0-9]{4}", parts[1]) is not None
                         and all(part == "1" for part in parts[2:-1]))
        if not pin_step and not verified_step:
            return _result()
        response = "exit" if parts[-1] == "0" else (
            "pin_verified" if verified_step and parts[-1] == "1" else "invalid_selection")
        phone, key, tag, secret = _binding(phone_number, session_id, service_code)
        digest = fingerprint_input(secret, key, tag, text)
        with ussd_transaction() as store:
            farmer, credential, session = _load(store, phone, key)
            _check_session(session, tag, service_code, _now())
            farmer_id = farmer["farmer_id"] if farmer is not None else None
            if session["farmer_id"] != farmer_id:
                raise _Refused("USSD verification is unavailable.")
            # Validate the genuine complete path, not just supplied cumulative text.
            for index in range(position):
                prefix = "*".join(parts[:index])
                code = "welcome" if index == 0 else "pin_prompt" if index == 1 else "pin_verified"
                _check_prefix(store, key, index, fingerprint_input(secret, key, tag, prefix), code)
            previous = store.get_callback_result(key, position)
            now = _now()  # All row/prefix/callback reads precede the decision.
            _check_session(session, tag, service_code, now)
            if previous is not None:
                if (session["last_input_count"] != position
                        or not hmac.compare_digest(previous["input_fingerprint"], digest)
                        or previous["response_text"] != SAFE_RESPONSES[response] or previous["response_status"] != 200):
                    raise _Refused("USSD verification is unavailable.")
                if response == "pin_verified":
                    _verified_menu_state(farmer, credential, session, now)
                elif session["state"] != "ended":
                    raise _Refused("USSD verification is unavailable.")
                return _result(response)  # Read-only recognition, no renewed authorization.
            if session["last_input_count"] != position - 1:
                raise _Refused("USSD verification is unavailable.")
            if pin_step:
                if (session["state"] != "pin" or session["verified_pin_version"] is not None
                        or session["authenticated_at"] is not None):
                    raise _Refused("USSD verification is unavailable.")
            else:
                _verified_menu_state(farmer, credential, session, now)
            store.store_callback_result(key, position, digest, response, now,
                                        expected_session=session, end_session=response != "pin_verified")
            # No additional SQL after this fresh expiry/lockout check.
            now = _now()
            _check_session(session, tag, service_code, now)
            if verified_step:
                _verified_menu_state(farmer, credential, session, now)
        return _result(response)
    except Exception:
        return _result()


def _verification_state(store, phone, session_key, phone_tag, service_code, fingerprint, prefix, welcome):
    farmer, credential, session = _load(store, phone, session_key)
    now = _now()
    _check_session(session, phone_tag, service_code, now)
    _check_credentials(credential, farmer)
    if session["farmer_id"] != farmer["farmer_id"]:
        raise _Refused("USSD verification is unavailable.")
    _check_prefix(store, session_key, 0, welcome, "welcome")
    _check_prefix(store, session_key, 1, prefix, "pin_prompt")
    cached = _duplicate(store, session_key, 2, fingerprint, session, credential)
    if cached is not None:
        return farmer, credential, session, now, cached
    if (session["state"] != "pin" or session["last_input_count"] != 1
            or session["verified_pin_version"] is not None or session["authenticated_at"] is not None):
        raise _Refused("USSD verification is unavailable.")
    if credential["locked_until"] is not None and _stamp(credential["locked_until"]) > now:
        raise _Refused("USSD verification is unavailable.")
    return farmer, credential, session, now, None


def authenticate_ussd_pin(phone_number, session_id, service_code, text):
    """Verify exactly `1*dddd` once per dial, without holding locks for scrypt.

    Caller IDs, input positions, hashes, and verification decisions are never
    accepted from the client. Failures share one fixed public response.
    """
    try:
        if not isinstance(text, str) or not re.fullmatch(r"1\*[0-9]{4}", text):
            return _result()
        phone, session_key, phone_tag, key = _binding(phone_number, session_id, service_code)
        current = fingerprint_input(key, session_key, phone_tag, text)
        prefix = fingerprint_input(key, session_key, phone_tag, "1")
        welcome = fingerprint_input(key, session_key, phone_tag, "")
        with ussd_transaction() as store:
            farmer, credential, session, _, cached = _verification_state(
                store, phone, session_key, phone_tag, service_code, current, prefix, welcome)
            if cached is not None:
                return cached
            identity = (farmer["user_id"], farmer["farmer_id"])
            snapshot = (credential["pin_hash"], credential["pin_key_version"], credential["pin_version"])
            lifetime = (session["created_at"], session["expires_at"], session["retain_until"])
        # Both public crypto helpers enforce the shared non-waiting capacity gate.
        matched = verify_ussd_pin(identity[1], text[2:], snapshot[0], snapshot[1])
        if type(matched) is not bool:
            raise _Refused("USSD verification is unavailable.")
        with ussd_transaction() as store:
            farmer, credential, session, now, cached = _verification_state(
                store, phone, session_key, phone_tag, service_code, current, prefix, welcome)
            if cached is not None:
                return cached
            if (identity != (farmer["user_id"], farmer["farmer_id"])
                    or snapshot != (credential["pin_hash"], credential["pin_key_version"], credential["pin_version"])
                    or lifetime != (session["created_at"], session["expires_at"], session["retain_until"])):
                raise _Refused("USSD verification is unavailable.")
            # Callback/prefix reads can wait after the initial lifetime check.
            # Decide from fresh time; the repository also guards the final SQL
            # transition with database UTC time against subsequent delays.
            now = _now()
            _check_session(session, phone_tag, service_code, now)
            if credential["locked_until"] is not None and _stamp(credential["locked_until"]) > now:
                raise _Refused("USSD verification is unavailable.")
            # Recompute from authoritative locked state, never the old snapshot.
            attempts = credential["failed_attempts"] if credential["locked_until"] is None else 0
            attempts = 0 if matched else attempts + 1
            until = now + timedelta(minutes=15) if attempts == 3 else None
            store.set_attempt_state(identity[1], attempts, until, now)
            store.store_callback_result(session_key, 2, current,
                                        "pin_verified" if matched else "verification_failed", now)
            if matched:
                store.mark_verified(session_key, identity[1], snapshot[2], now)
            else:
                store.set_session_state(session_key, "ended", now)
        return _result("pin_verified", authenticated=True) if matched else _result()
    except Exception:
        # Includes capacity/configuration, corrupt rows, conflicts, SQL failures.
        # No request bodies, PINs, identifiers, hashes, or exception details logged.
        return _result()
