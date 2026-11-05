"""Callback dispatch through the existing two-phase authentication service.

Trusted caller must enforce ingress and shared admission before calling this
internal orchestration API. Never wrap authentication calls in an outer unit.
"""

import hmac
import re
from datetime import datetime, timedelta, timezone

from app.models.ussd_fingerprints import derive_session_key, fingerprint_input, fingerprint_phone
from app.models.ussd_repository import SAFE_RESPONSES, UssdConflictError, UssdPersistenceError, ussd_transaction
from app.services.registration_service import normalize_kenyan_phone
from app.services import ussd_authentication_service as authentication


def _now():
    return datetime.now(timezone.utc)


def _stamp(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _live_unbound(session, tag, code, *, ended=False):
    now = _now()  # Read after database operations/lock waits, never before them.
    if (session is None or session["state"] not in (("welcome", "ended") if ended else ("welcome",))
            or session["farmer_id"] is not None
            or session["verified_pin_version"] is not None or session["authenticated_at"] is not None
            or not hmac.compare_digest(session["phone_tag"], tag) or session["service_code"] != code
            or not _stamp(session["created_at"]) <= now < _stamp(session["expires_at"])
            or _stamp(session["retain_until"]) < _stamp(session["expires_at"])):
        raise UssdConflictError("USSD callback state refused.")
    return now


def _initial_result(store, key, digest):
    previous = store.get_callback_result(key, 0)
    if (previous is None
            or not hmac.compare_digest(previous["input_fingerprint"], digest)
            or previous["response_text"] != SAFE_RESPONSES["welcome"] or previous["response_status"] != 200):
        raise UssdConflictError("USSD callback history refused.")


def _welcome_replay(store, session, key, tag, code, digest):
    _initial_result(store, key, digest)
    _live_unbound(session, tag, code)
    if session["last_input_count"] != 0:
        raise UssdConflictError("USSD callback position refused.")
    return SAFE_RESPONSES["welcome"]


def orchestrate_callback(settings, session_id, service_code, phone_number, text):
    """Return fixed public text only after transaction completion.

    An initial-insert failure gets one read-only reconciliation, never an insert
    retry. This handles a winning concurrent insert/lost commit acknowledgement
    only when the full live initial binding/result can be independently read.
    No service success is authorization and no stored history is synthesized.
    """
    phone = normalize_kenyan_phone(phone_number)
    if (phone is None or service_code != settings.service_code or not isinstance(text, str)
            or len(text) > 128 or text.count("*") > 15
            or (text and not re.fullmatch(r"[0-9]+(?:\*[0-9]+)*", text))):
        return SAFE_RESPONSES["invalid_request"]
    try:
        key = derive_session_key(settings.metadata_key, service_code, session_id)
        tag = fingerprint_phone(settings.metadata_key, phone)
        digest = fingerprint_input(settings.metadata_key, key, tag, text)
        # Services own their transactions and release all row locks for scrypt.
        # Response text, including cached success, is never an authorization grant.
        if text == "1":
            return authentication.identify_ussd_candidate(phone, session_id, service_code)["response"]
        if re.fullmatch(r"1\*[0-9]{4}", text):
            return authentication.authenticate_ussd_pin(phone, session_id, service_code, text)["response"]
        if "*" in text:
            return authentication.advance_ussd_menu(phone, session_id, service_code, text)["response"]
        if text == "":
            try:
                with ussd_transaction() as store:
                    session = store.get_session(key)
                    if session is not None:
                        return _welcome_replay(store, session, key, tag, service_code, digest)
                    now = _now()
                    expiry = now + timedelta(seconds=settings.session_seconds)
                    store.create_session(key, tag, service_code, now,
                                         expiry,
                                         now + timedelta(seconds=settings.retention_seconds))
                    store.store_callback_result(key, 0, digest, "welcome", _now())
                    if _now() >= expiry:
                        raise UssdConflictError("USSD session has expired.")
                return SAFE_RESPONSES["welcome"]
            except UssdConflictError:
                raise
            except UssdPersistenceError:
                # No writes in reconciliation, and no loop on operational errors.
                with ussd_transaction() as store:
                    session = store.get_session(key)
                    return _welcome_replay(store, session, key, tag, service_code, digest)
        # Unbound welcome exit/invalid selection needs no account lookup.
        with ussd_transaction() as store:
            session = store.get_session(key)
            # Reject bound rows before reading credentials: this foundation never
            # takes a credential lock after a session lock, nor authenticates.
            _live_unbound(session, tag, service_code, ended=True)
            prefix = fingerprint_input(settings.metadata_key, key, tag, "")
            _initial_result(store, key, prefix)
            previous = store.get_callback_result(key, 1)
            response = "exit" if text == "0" else "invalid_selection"
            _live_unbound(session, tag, service_code, ended=True)
            if (session["state"] == "ended" and session["last_input_count"] == 1 and previous is not None
                    and hmac.compare_digest(previous["input_fingerprint"], digest)
                    and previous["response_text"] == SAFE_RESPONSES[response] and previous["response_status"] == 200):
                return SAFE_RESPONSES[response]  # Recognition only; never reopens/advances.
            if previous is not None or session["last_input_count"] != 0:
                raise UssdConflictError("USSD callback position refused.")
            now = _live_unbound(session, tag, service_code)
            store.store_callback_result(key, 1, digest, response, now,
                                        expected_session=session, end_session=True)
            _live_unbound(session, tag, service_code)  # Fresh check after all writes.
        return SAFE_RESPONSES[response]
    except Exception:
        # No diagnostic chaining, callback bodies, identities or secrets logged.
        return SAFE_RESPONSES["verification_failed"]
