"""Transaction-scoped USSD storage; no authentication or HTTP integration.

Use `with ussd_transaction() as store:`. Methods never commit independently.
Future authentication must lock credentials, then session, then callback result,
and check duplicates before changing counters. Storage is not authorization.
"""

import hmac
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps

from app.db import get_db_connection


logger = logging.getLogger(__name__)

# Only vetted, fixed text may be persisted in this stage. No caller interpolation.
SAFE_RESPONSES = {
    "welcome": "CON Welcome to FarmAdvis\n1. Request maize advice\n0. Exit",
    "pin_prompt": "CON Enter your 4-digit FarmAdvis PIN\n0. Exit",
    "exit": "END Thank you for using FarmAdvis.",
    "verification_failed": "END Verification could not be completed. Please use the farmer portal or try again later.",
    "verification_unavailable": "END Account verification is not yet available. Please try again later.",
    "invalid_request": "END Invalid request. Please dial again.",
    "invalid_selection": "END Invalid selection. Please dial again.",
}
CREDENTIAL_COLUMNS = (
    "farmer_id, pin_hash, pin_key_version, pin_version, failed_attempts, "
    "locked_until, web_failed_attempts, web_locked_until, created_at, updated_at"
)
SESSION_COLUMNS = (
    "session_key, phone_tag, service_code, farmer_id, state, verified_pin_version, "
    "authenticated_at, last_input_count, created_at, expires_at, retain_until"
)


class UssdPersistenceError(Exception):
    """Safe storage failure: never contains SQL diagnostics or input values."""


class UssdConflictError(UssdPersistenceError):
    """State mismatch; the complete transaction must be rolled back."""


class UssdValidationError(ValueError):
    """Explicitly trusted validation failure with a fixed, non-sensitive message."""


def _operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if not self._active or self._rollback_only:
            raise UssdPersistenceError("USSD transaction is not available.")
        try:
            return method(self, *args, **kwargs)
        except (UssdValidationError, UssdPersistenceError):
            self._rollback_only = True
            self._failed = True
            raise
        except Exception:
            self._rollback_only = True
            self._failed = True
            raise UssdPersistenceError("USSD storage operation failed.") from None
    return guarded


def _id(value):
    if type(value) is not int or not 0 < value <= 18446744073709551615:
        raise UssdValidationError("A valid farmer ID is required.")


def _digest(value):
    if not isinstance(value, bytes) or len(value) != 32:
        raise UssdValidationError("A 32-byte keyed fingerprint is required.")


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise UssdValidationError("An aware UTC-compatible timestamp is required.")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def session_expired(record, now):
    """MariaDB DATETIME values are stored and interpreted as UTC."""
    expiry = record["expires_at"]
    if expiry.tzinfo is not None:
        expiry = _utc(expiry)
    return record["state"] == "ended" or _utc(now) >= expiry


class _UssdRepository:
    def __init__(self, cursor):
        self._cursor = cursor
        self._active = True
        self._rollback_only = False
        self._failed = False

    @_operation
    def get_credentials(self, farmer_id):
        _id(farmer_id)
        self._cursor.execute(
            "SELECT " + CREDENTIAL_COLUMNS + " FROM farmer_ussd_credentials "
            "WHERE farmer_id = %s FOR UPDATE", (farmer_id,),
        )
        return self._cursor.fetchone()

    @_operation
    def ensure_credentials(self, farmer_id, now):
        _id(farmer_id)
        stamp = _utc(now)
        self._cursor.execute(
            "INSERT INTO farmer_ussd_credentials (farmer_id, created_at, updated_at) "
            "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE farmer_id = farmer_id",
            (farmer_id, stamp, stamp),
        )
        return self.get_credentials(farmer_id)

    @_operation
    def replace_pin_hash(self, farmer_id, pin_hash, pin_key_version, now):
        # Validate the stored representation only; hashing/verification is Stage 2C.
        if (not isinstance(pin_hash, str) or len(pin_hash) > 255
                or not re.fullmatch(r"scrypt:[0-9]+:[0-9]+:[0-9]+\$[A-Za-z0-9]{1,64}\$[0-9a-f]{128}", pin_hash)
                or type(pin_key_version) is not int or not 1 <= pin_key_version <= 65535):
            raise UssdValidationError("A supported encoded hash and key version are required.")
        self.ensure_credentials(farmer_id, now)
        self._cursor.execute(
            "UPDATE farmer_ussd_credentials SET pin_hash = %s, pin_key_version = %s, "
            "pin_version = pin_version + 1, failed_attempts = 0, locked_until = NULL, "
            "web_failed_attempts = 0, web_locked_until = NULL, updated_at = %s "
            "WHERE farmer_id = %s", (pin_hash, pin_key_version, _utc(now), farmer_id),
        )
        if self._cursor.rowcount != 1:
            raise UssdConflictError("USSD credentials changed unexpectedly.")
        self.end_farmer_sessions(farmer_id)
        return self.get_credentials(farmer_id)

    @_operation
    def set_attempt_state(self, farmer_id, failed_attempts, locked_until, now, *, web=False):
        _id(farmer_id)
        if type(web) is not bool or type(failed_attempts) is not int or not 0 <= failed_attempts <= (5 if web else 3):
            raise UssdValidationError("Invalid attempt state.")
        until = None if locked_until is None else _utc(locked_until)
        if self.get_credentials(farmer_id) is None:
            raise UssdConflictError("USSD credentials are unavailable.")
        if web:
            sql = ("UPDATE farmer_ussd_credentials SET web_failed_attempts = %s, "
                   "web_locked_until = %s, updated_at = %s WHERE farmer_id = %s")
        else:
            sql = ("UPDATE farmer_ussd_credentials SET failed_attempts = %s, "
                   "locked_until = %s, updated_at = %s WHERE farmer_id = %s")
        self._cursor.execute(sql, (failed_attempts, until, _utc(now), farmer_id))

    @_operation
    def create_session(self, session_key, phone_tag, service_code, now, expires_at, retain_until, *, farmer_id=None):
        _digest(session_key)
        _digest(phone_tag)
        if farmer_id is not None:
            _id(farmer_id)
        if (not isinstance(service_code, str) or len(service_code) > 40
                or not re.fullmatch(r"\*[0-9]{1,6}(?:\*[0-9]{1,6})*#", service_code)):
            raise UssdValidationError("Invalid service code.")
        created, expiry, retention = _utc(now), _utc(expires_at), _utc(retain_until)
        if not created < expiry <= retention:
            raise UssdValidationError("Invalid session lifetime.")
        self._cursor.execute(
            "INSERT INTO ussd_sessions (session_key, phone_tag, service_code, farmer_id, "
            "state, created_at, expires_at, retain_until) VALUES (%s, %s, %s, %s, 'welcome', %s, %s, %s)",
            (session_key, phone_tag, service_code, farmer_id, created, expiry, retention),
        )

    @_operation
    def get_session(self, session_key):
        _digest(session_key)
        self._cursor.execute("SELECT " + SESSION_COLUMNS + " FROM ussd_sessions WHERE session_key = %s FOR UPDATE",
                             (session_key,))
        return self._cursor.fetchone()

    @_operation
    def set_session_state(self, session_key, state, now):
        if state not in ("pin", "ended"):
            raise UssdValidationError("Use verification metadata to enter the verified state.")
        record = self.get_session(session_key)
        if record is None or (state == "pin" and (record["state"] != "welcome" or session_expired(record, now))):
            raise UssdConflictError("USSD session transition refused.")
        _utc(now)
        self._cursor.execute(
            "UPDATE ussd_sessions SET state = %s, authenticated_at = NULL, verified_pin_version = NULL "
            "WHERE session_key = %s", (state, session_key),
        )

    @_operation
    def mark_verified(self, session_key, farmer_id, pin_version, now):
        # Lock credentials before sessions, consistently with PIN reset.
        credential = self.get_credentials(farmer_id)
        record = self.get_session(session_key)
        if (type(pin_version) is not int or pin_version <= 0 or credential is None
                or credential["pin_hash"] is None or credential["pin_version"] != pin_version
                or record is None or record["farmer_id"] != farmer_id
                or record["state"] != "pin" or session_expired(record, now)):
            raise UssdConflictError("USSD verification metadata refused.")
        self._cursor.execute(
            "UPDATE ussd_sessions SET state = 'verified', verified_pin_version = %s, authenticated_at = %s "
            "WHERE session_key = %s", (pin_version, _utc(now), session_key),
        )

    @_operation
    def end_farmer_sessions(self, farmer_id):
        _id(farmer_id)
        self.get_credentials(farmer_id)
        self._cursor.execute(
            "UPDATE ussd_sessions SET state = 'ended', authenticated_at = NULL, verified_pin_version = NULL "
            "WHERE farmer_id = %s AND state <> 'ended'", (farmer_id,),
        )

    @_operation
    def get_callback_result(self, session_key, input_count):
        _digest(session_key)
        if type(input_count) is not int or not 0 <= input_count <= 16:
            raise UssdValidationError("Invalid input position.")
        self._cursor.execute(
            "SELECT input_fingerprint, response_text, response_status, created_at "
            "FROM ussd_callback_results WHERE session_key = %s AND input_count = %s FOR UPDATE",
            (session_key, input_count),
        )
        return self._cursor.fetchone()

    @_operation
    def store_callback_result(self, session_key, input_count, input_fingerprint, response_code, now, *, status=200):
        """Store terminal results before ending the session in this transaction.

        An existing exact duplicate is recognition only: it makes the entire
        unit rollback-only, including for ended or PIN-reset-invalidated sessions.
        """
        _digest(input_fingerprint)
        if (not isinstance(response_code, str) or response_code not in SAFE_RESPONSES
                or type(status) is not int or status not in (200, 400, 403, 413, 415, 503)):
            raise UssdValidationError("A catalog response and supported status are required.")
        record = self.get_session(session_key)
        if record is None:
            raise UssdConflictError("USSD session is unavailable.")
        previous = self.get_callback_result(session_key, input_count)
        text = SAFE_RESPONSES[response_code]
        if previous is not None:
            if (not hmac.compare_digest(previous["input_fingerprint"], input_fingerprint)
                    or previous["response_text"] != text or previous["response_status"] != status):
                raise UssdConflictError("USSD callback conflicts with an accepted result.")
            # Roll back even if a future caller mutated counters before detecting
            # this duplicate. Never commit those changes on the duplicate path.
            self._rollback_only = True
            return False
        if record["state"] == "ended":
            raise UssdConflictError("USSD session has ended.")
        if input_count != record["last_input_count"] + 1:
            raise UssdConflictError("USSD callback position is out of order.")
        if _utc(now) >= record["expires_at"]:
            raise UssdConflictError("USSD session has expired.")
        self._cursor.execute(
            "INSERT INTO ussd_callback_results (session_key, input_count, input_fingerprint, "
            "response_text, response_status, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (session_key, input_count, input_fingerprint, text, status, _utc(now)),
        )
        self._cursor.execute("UPDATE ussd_sessions SET last_input_count = %s WHERE session_key = %s",
                             (input_count, session_key))
        return True

    @_operation
    def cleanup_retained_sessions(self, now, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise UssdValidationError("Invalid cleanup batch size.")
        self._cursor.execute(
            "DELETE FROM ussd_sessions WHERE retain_until <= %s AND expires_at <= %s "
            "ORDER BY retain_until, session_key LIMIT %s", (_utc(now), _utc(now), limit),
        )
        # Callback results are removed only through the FK cascade.
        return self._cursor.rowcount


@contextmanager
def ussd_transaction():
    """Own one connection and transaction. Never log exception diagnostics.

    A method failure poisons the transaction even if the caller catches it.
    Exact duplicates make the unit rollback-only and return False from store.
    A commit failure has an uncertain outcome; callers must reconcile by key.
    """
    connection = cursor = repository = None
    try:
        connection = get_db_connection()
        connection.start_transaction()
        cursor = connection.cursor(dictionary=True)
        repository = _UssdRepository(cursor)
        yield repository
        if repository._failed:
            raise UssdPersistenceError("USSD transaction was invalidated.")
        if repository._rollback_only:
            connection.rollback()
        else:
            connection.commit()
    except Exception as error:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                logger.error("USSD transaction rollback failed.")
        if isinstance(error, (UssdValidationError, UssdPersistenceError)):
            raise
        raise UssdPersistenceError("USSD transaction could not be confirmed.") from None
    finally:
        if repository is not None:
            repository._active = False
        for label, resource in (("cursor", cursor), ("connection", connection)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    logger.warning("USSD transaction %s cleanup failed.", label)
