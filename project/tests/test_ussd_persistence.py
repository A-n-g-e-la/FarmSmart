import unittest
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error, IntegrityError

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app.models.ussd_fingerprints import derive_session_key, fingerprint_input, fingerprint_phone
    from app.models.ussd_repository import (
        SAFE_RESPONSES, UssdConflictError, UssdPersistenceError, UssdValidationError,
        session_expired, ussd_transaction,
    )


NOW = datetime(2026, 10, 29, 8, tzinfo=timezone.utc)
# Synthetic fixtures only: no secret generation and no real PIN hashing.
METADATA_KEY = b"synthetic-metadata-test-key-32-bytes"
SESSION_KEY = derive_session_key(METADATA_KEY, "*384*123#", "synthetic-provider-session")
PHONE_TAG = fingerprint_phone(METADATA_KEY, "+254712345678")
INPUT_TAG = fingerprint_input(METADATA_KEY, SESSION_KEY, PHONE_TAG, "1*0042")
PIN_HASH = "scrypt:32768:8:1$SyntheticSalt$" + "a" * 128


class FingerprintTests(unittest.TestCase):
    def test_stable_keyed_and_bound_fingerprints(self):
        self.assertEqual(len(SESSION_KEY), 32)
        self.assertEqual(SESSION_KEY, derive_session_key(METADATA_KEY, "*384*123#", "synthetic-provider-session"))
        self.assertNotEqual(SESSION_KEY, derive_session_key(b"different-test-secret-of-32-bytes", "*384*123#", "synthetic-provider-session"))
        self.assertNotEqual(SESSION_KEY, derive_session_key(METADATA_KEY, "*123#", "synthetic-provider-session"))
        other_phone = fingerprint_phone(METADATA_KEY, "+254712345679")
        self.assertNotEqual(PHONE_TAG, other_phone)
        self.assertNotEqual(INPUT_TAG, fingerprint_input(METADATA_KEY, SESSION_KEY, other_phone, "1*0042"))
        self.assertNotEqual(INPUT_TAG, fingerprint_input(METADATA_KEY, b"s" * 32, PHONE_TAG, "1*0042"))
        self.assertNotEqual(INPUT_TAG, fingerprint_input(METADATA_KEY, SESSION_KEY, PHONE_TAG, "1*0043"))

    def test_keys_and_raw_inputs_are_bounded(self):
        for secret in (None, "not-bytes", b"short"):
            with self.subTest(secret=secret), self.assertRaises(ValueError):
                derive_session_key(secret, "*123#", "session")
        for phone in ("0712345678", "invalid", "" ):
            with self.subTest(phone=phone), self.assertRaises(ValueError):
                fingerprint_phone(METADATA_KEY, phone)
        for text in ("raw-password", "1" * 129, "1**2", "*".join(["1"] * 17)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                fingerprint_input(METADATA_KEY, SESSION_KEY, PHONE_TAG, text)
        with self.assertRaises(ValueError):
            derive_session_key(METADATA_KEY, "bad-code", "session")
        with self.assertRaises(ValueError):
            derive_session_key(METADATA_KEY, "*123#", "bad\nsession")


class UssdPersistenceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.rowcount = 1
        helper = patch("app.models.ussd_repository.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)
        self.credentials = {"farmer_id": 91, "pin_hash": PIN_HASH, "pin_version": 2}
        self.session = {
            "session_key": SESSION_KEY, "phone_tag": PHONE_TAG, "farmer_id": 91,
            "state": "welcome", "last_input_count": -1,
            "expires_at": (NOW + timedelta(minutes=3)).replace(tzinfo=None),
        }
        self.previous = None
        self.cursor.fetchone.side_effect = self.fetch

    def fetch(self):
        sql = self.cursor.execute.call_args.args[0]
        if "FROM farmer_ussd_credentials" in sql:
            return self.credentials
        if "FROM ussd_callback_results" in sql:
            return self.previous
        if "FROM ussd_sessions" in sql:
            return self.session
        self.fail("Unexpected mocked SELECT")

    def tearDown(self):
        self.live_connector.assert_not_called()
        # Every statement binds values separately, including DELETE limits.
        for call in self.cursor.execute.call_args_list:
            sql, params = call.args
            self.assertIsInstance(params, tuple)
            self.assertEqual(sql.count("%s"), len(params))
            self.assertNotIn(PIN_HASH, sql)

    def test_retrieve_and_ensure_unenrolled_credentials(self):
        with ussd_transaction() as store:
            self.assertEqual(store.get_credentials(91), self.credentials)
            store.ensure_credentials(91, NOW)
        calls = self.cursor.execute.call_args_list
        self.assertIn("FOR UPDATE", calls[0].args[0])
        self.assertEqual(calls[0].args[1], (91,))
        self.assertIn("ON DUPLICATE KEY UPDATE farmer_id = farmer_id", calls[1].args[0])
        self.assertNotIn("pin_hash", calls[1].args[0])
        self.connection.start_transaction.assert_called_once_with()
        self.connection.commit.assert_called_once_with()
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_pin_change_increments_version_and_invalidates_sessions_in_one_transaction(self):
        def execute(sql, params):
            if sql.startswith("UPDATE farmer_ussd_credentials SET pin_hash"):
                self.credentials = dict(self.credentials, pin_version=3)
        self.cursor.execute.side_effect = execute
        with ussd_transaction() as store:
            result = store.replace_pin_hash(91, PIN_HASH, 2, NOW)
        self.assertEqual(result["pin_version"], 3)
        calls = self.cursor.execute.call_args_list
        update = next(call for call in calls if "pin_version = pin_version + 1" in call.args[0])
        self.assertEqual(update.args[1], (PIN_HASH, 2, NOW.replace(tzinfo=None), 91))
        self.assertIn("failed_attempts = 0, locked_until = NULL", update.args[0])
        self.assertTrue(any("WHERE farmer_id = %s AND state <> 'ended'" in call.args[0] for call in calls))
        self.connection.commit.assert_called_once_with()

    def test_pin_reset_session_failure_rolls_back_every_change(self):
        def execute(sql, params):
            if sql.startswith("UPDATE ussd_sessions"):
                raise Error("private-sql-parameters")
        self.cursor.execute.side_effect = execute
        with self.assertRaises(UssdPersistenceError) as error:
            with ussd_transaction() as store:
                store.replace_pin_hash(91, PIN_HASH, 1, NOW)
        self.assertNotIn("private", str(error.exception))
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_plaintext_and_unsupported_hashes_are_rejected_before_sql(self):
        for value in ("0042", "1*0042", "encrypted:0042", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                with ussd_transaction() as store:
                    store.replace_pin_hash(91, value, 1, NOW)
        self.cursor.execute.assert_not_called()
        self.connection.commit.assert_not_called()

    def test_attempt_and_lockout_storage_for_both_channels(self):
        until = NOW + timedelta(minutes=15)
        with ussd_transaction() as store:
            store.set_attempt_state(91, 3, until, NOW)
            store.set_attempt_state(91, 5, until, NOW, web=True)
        updates = [call.args for call in self.cursor.execute.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertIn("SET failed_attempts = %s, locked_until = %s", updates[0][0])
        self.assertIn("SET web_failed_attempts = %s, web_locked_until = %s", updates[1][0])
        self.assertEqual(updates[0][1], (3, until.replace(tzinfo=None), NOW.replace(tzinfo=None), 91))

    def test_missing_credentials_cannot_be_updated(self):
        self.credentials = None
        with self.assertRaises(UssdConflictError):
            with ussd_transaction() as store:
                store.set_attempt_state(91, 1, None, NOW)
        self.connection.commit.assert_not_called()

    def test_invalid_attempts_and_ids_are_rejected(self):
        for farmer, count in ((True, 0), (0, 0), ("91 OR 1=1", 0), (91, True), (91, 4), (91, -1)):
            with self.subTest(farmer=farmer, count=count), self.assertRaises(ValueError):
                with ussd_transaction() as store:
                    store.set_attempt_state(farmer, count, None, NOW)
        self.cursor.execute.assert_not_called()

    def test_session_creation_uses_only_derived_bindings_and_absolute_times(self):
        expiry, retention = NOW + timedelta(minutes=3), NOW + timedelta(hours=24)
        with ussd_transaction() as store:
            store.create_session(SESSION_KEY, PHONE_TAG, "*384*123#", NOW, expiry, retention, farmer_id=91)
            self.assertEqual(store.get_session(SESSION_KEY), self.session)
        params = self.cursor.execute.call_args_list[0].args[1]
        self.assertEqual(params, (SESSION_KEY, PHONE_TAG, "*384*123#", 91,
                                  NOW.replace(tzinfo=None), expiry.replace(tzinfo=None), retention.replace(tzinfo=None)))
        self.assertNotIn("+254712345678", repr(params))
        self.assertNotIn("synthetic-provider-session", repr(params))

    def test_raw_identifiers_and_bad_lifetimes_are_rejected(self):
        cases = [("raw-session", PHONE_TAG, NOW + timedelta(minutes=3), NOW + timedelta(hours=24)),
                 (SESSION_KEY, "+254712345678", NOW + timedelta(minutes=3), NOW + timedelta(hours=24)),
                 (SESSION_KEY, PHONE_TAG, NOW, NOW + timedelta(hours=24)),
                 (SESSION_KEY, PHONE_TAG, NOW + timedelta(minutes=3), NOW)]
        for key, phone, expiry, retention in cases:
            with self.subTest(key=key), self.assertRaises(ValueError):
                with ussd_transaction() as store:
                    store.create_session(key, phone, "*123#", NOW, expiry, retention)
        self.cursor.execute.assert_not_called()

    def test_expiry_boundary_and_ended_sessions(self):
        self.assertFalse(session_expired(self.session, NOW))
        self.assertTrue(session_expired(self.session, NOW + timedelta(minutes=3)))
        self.assertTrue(session_expired(dict(self.session, state="ended"), NOW))
        with self.assertRaises(ValueError):
            session_expired(self.session, NOW.replace(tzinfo=None))

    def test_pin_transition_and_single_session_end(self):
        with ussd_transaction() as store:
            store.set_session_state(SESSION_KEY, "pin", NOW)
            store.set_session_state(SESSION_KEY, "ended", NOW)
        updates = [call.args for call in self.cursor.execute.call_args_list if call.args[0].startswith("UPDATE")]
        self.assertEqual(updates[0][1], ("pin", SESSION_KEY))
        self.assertEqual(updates[1][1], ("ended", SESSION_KEY))
        self.assertIn("authenticated_at = NULL", updates[1][0])

    def test_verification_metadata_requires_matching_credential_and_candidate(self):
        self.session["state"] = "pin"
        with ussd_transaction() as store:
            store.mark_verified(SESSION_KEY, 91, 2, NOW)
        sql, params = self.cursor.execute.call_args.args
        self.assertEqual(params, (2, SESSION_KEY, 91, 91, 2))
        for predicate in ("authenticated_at = UTC_TIMESTAMP(6)", "state = 'pin'",
                          "farmer_id = %s", "verified_pin_version IS NULL", "authenticated_at IS NULL",
                          "created_at <= UTC_TIMESTAMP(6)", "expires_at > UTC_TIMESTAMP(6)",
                          "credential.pin_version = %s", "credential.pin_hash IS NOT NULL",
                          "credential.locked_until <= UTC_TIMESTAMP(6)"):
            self.assertIn(predicate, sql)
        for changes in ({"farmer_id": 92}, {"state": "ended"}, {"expires_at": NOW.replace(tzinfo=None)}):
            original = dict(self.session)
            self.session.update(changes)
            with self.subTest(changes=changes), self.assertRaises(UssdConflictError):
                with ussd_transaction() as store:
                    store.mark_verified(SESSION_KEY, 91, 2, NOW)
            self.session = original
        with self.assertRaises(UssdConflictError):
            with ussd_transaction() as store:
                store.mark_verified(SESSION_KEY, 91, 1, NOW)

    def test_zero_row_verification_transition_poisons_transaction_even_if_caught(self):
        self.session["state"] = "pin"
        self.cursor.rowcount = 0
        with self.assertRaises(UssdPersistenceError):
            with ussd_transaction() as store:
                with self.assertRaisesRegex(UssdConflictError, "USSD verification transition refused"):
                    store.mark_verified(SESSION_KEY, 91, 2, NOW)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_verified_state_cannot_be_set_without_metadata(self):
        with self.assertRaises(ValueError):
            with ussd_transaction() as store:
                store.set_session_state(SESSION_KEY, "verified", NOW)
        self.cursor.execute.assert_not_called()

    def test_farmer_session_invalidation_is_owned_and_clears_metadata(self):
        with ussd_transaction() as store:
            store.end_farmer_sessions(91)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("WHERE farmer_id = %s", sql)
        self.assertIn("verified_pin_version = NULL", sql)
        self.assertEqual(params, (91,))

    def test_callback_insert_updates_position_and_does_not_persist_raw_history(self):
        with ussd_transaction() as store:
            self.assertTrue(store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, "welcome", NOW))
        insert = next(call.args for call in self.cursor.execute.call_args_list if call.args[0].startswith("INSERT"))
        self.assertEqual(insert[1], (SESSION_KEY, 0, INPUT_TAG, SAFE_RESPONSES["welcome"], 200, NOW.replace(tzinfo=None)))
        self.assertNotIn("0042", repr(self.cursor.execute.call_args_list))
        self.assertNotIn("1*0042", repr(self.cursor.execute.call_args_list))
        self.assertEqual(self.cursor.execute.call_args.args[1], (0, SESSION_KEY))

    def test_exact_callback_duplicate_rolls_back_even_prior_counter_changes(self):
        self.previous = {"input_fingerprint": INPUT_TAG, "response_text": SAFE_RESPONSES["verification_failed"], "response_status": 200}
        with ussd_transaction() as store:
            store.set_attempt_state(91, 1, None, NOW)
            self.assertFalse(store.store_callback_result(SESSION_KEY, 2, INPUT_TAG, "verification_failed", NOW))
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.assertFalse(any(call.args[0].startswith("INSERT") for call in self.cursor.execute.call_args_list))

    def test_conflicting_replay_cannot_overwrite_callback(self):
        self.previous = {"input_fingerprint": b"x" * 32, "response_text": SAFE_RESPONSES["welcome"], "response_status": 200}
        with self.assertRaises(UssdConflictError):
            with ussd_transaction() as store:
                store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, "welcome", NOW)
        self.connection.commit.assert_not_called()

    def test_ended_session_cannot_accept_a_new_result_or_advance_position(self):
        self.session.update(state="ended", last_input_count=1)
        with self.assertRaisesRegex(UssdConflictError, "^USSD session has ended\\.$"):
            with ussd_transaction() as store:
                store.store_callback_result(SESSION_KEY, 2, INPUT_TAG, "exit", NOW)
        self.assertEqual(self.session["last_input_count"], 1)
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in self.cursor.execute.call_args_list))
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_exact_terminal_duplicate_is_recognized_after_end_and_expiry_without_authorizing_work(self):
        self.session.update(state="ended", last_input_count=1)
        self.previous = {"input_fingerprint": INPUT_TAG, "response_text": SAFE_RESPONSES["exit"], "response_status": 200}
        for now in (NOW, NOW + timedelta(minutes=4)):
            with self.subTest(now=now):
                self.connection.reset_mock()
                with ussd_transaction() as store:
                    self.assertFalse(store.store_callback_result(SESSION_KEY, 1, INPUT_TAG, "exit", now))
                    with self.assertRaises(UssdPersistenceError):
                        store.set_attempt_state(91, 1, None, now)
                self.assertTrue(all(call.args[0].startswith("SELECT") for call in self.cursor.execute.call_args_list))
                self.connection.rollback.assert_called_once_with()
                self.connection.commit.assert_not_called()

    def test_conflicting_terminal_duplicate_is_refused_after_session_end(self):
        self.session.update(state="ended", last_input_count=1)
        self.previous = {"input_fingerprint": b"x" * 32, "response_text": SAFE_RESPONSES["exit"], "response_status": 200}
        with self.assertRaises(UssdConflictError):
            with ussd_transaction() as store:
                store.store_callback_result(SESSION_KEY, 1, INPUT_TAG, "exit", NOW)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_terminal_result_is_stored_before_session_end_in_one_transaction(self):
        self.session["last_input_count"] = 0
        with ussd_transaction() as store:
            self.assertTrue(store.store_callback_result(SESSION_KEY, 1, INPUT_TAG, "exit", NOW))
            self.connection.commit.assert_not_called()
            store.set_session_state(SESSION_KEY, "ended", NOW)
            self.connection.commit.assert_not_called()
        sql = [call.args[0] for call in self.cursor.execute.call_args_list]
        insert = next(index for index, statement in enumerate(sql) if statement.startswith("INSERT"))
        position = next(index for index, statement in enumerate(sql) if "SET last_input_count" in statement)
        ending = next(index for index, statement in enumerate(sql) if "SET state = %s" in statement)
        self.assertLess(insert, position)
        self.assertLess(position, ending)
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()

    def test_terminal_session_end_failure_rolls_back_result_and_position(self):
        def execute(sql, params):
            if "SET state = %s" in sql:
                raise RuntimeError("synthetic-sensitive-internal-detail")
        self.cursor.execute.side_effect = execute
        with self.assertRaises(UssdPersistenceError):
            with ussd_transaction() as store:
                store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, "exit", NOW)
                store.set_session_state(SESSION_KEY, "ended", NOW)
        self.assertTrue(any(call.args[0].startswith("INSERT") for call in self.cursor.execute.call_args_list))
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_ending_before_storing_terminal_result_rolls_back(self):
        def execute(sql, params):
            if "SET state = %s" in sql:
                self.session["state"] = params[0]
        self.cursor.execute.side_effect = execute
        with self.assertRaises(UssdConflictError):
            with ussd_transaction() as store:
                store.set_session_state(SESSION_KEY, "ended", NOW)
                store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, "exit", NOW)
        self.assertFalse(any(call.args[0].startswith("INSERT") for call in self.cursor.execute.call_args_list))
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_pin_reset_invalidated_session_cannot_accept_new_callback_results(self):
        def execute(sql, params):
            if sql.startswith("UPDATE farmer_ussd_credentials SET pin_hash"):
                self.credentials["pin_version"] += 1
            elif sql.startswith("UPDATE ussd_sessions SET state = 'ended'"):
                self.session.update(state="ended", authenticated_at=None, verified_pin_version=None)
        self.cursor.execute.side_effect = execute
        with ussd_transaction() as store:
            store.replace_pin_hash(91, PIN_HASH, 1, NOW)
        self.connection.commit.assert_called_once_with()
        self.assertEqual(self.session["state"], "ended")
        self.connection.reset_mock()
        with self.assertRaises(UssdConflictError):
            with ussd_transaction() as store:
                store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, "welcome", NOW)
        self.assertTrue(all(call.args[0].startswith("SELECT") for call in self.cursor.execute.call_args_list))
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_callback_out_of_order_and_expired_records_are_refused(self):
        for position, now in ((2, NOW), (0, NOW + timedelta(minutes=3))):
            with self.subTest(position=position), self.assertRaises(UssdConflictError):
                with ussd_transaction() as store:
                    store.store_callback_result(SESSION_KEY, position, INPUT_TAG, "welcome", now)

    def test_arbitrary_response_text_cannot_reach_sql(self):
        for response in ("END PIN 0042", "1*0042", "private input"):
            with self.subTest(response=response), self.assertRaises(ValueError):
                with ussd_transaction() as store:
                    store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, response, NOW)
        self.cursor.execute.assert_not_called()

    def test_duplicate_key_insert_rolls_back_instead_of_overwriting(self):
        def execute(sql, params):
            if sql.startswith("INSERT"):
                raise IntegrityError("private duplicate", errno=1062)
        self.cursor.execute.side_effect = execute
        with self.assertRaises(UssdPersistenceError):
            with ussd_transaction() as store:
                store.store_callback_result(SESSION_KEY, 0, INPUT_TAG, "welcome", NOW)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_cleanup_is_bounded_and_only_removes_retained_expired_sessions(self):
        with ussd_transaction() as store:
            self.assertEqual(store.cleanup_retained_sessions(NOW, limit=20), 1)
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("retain_until <= %s AND expires_at <= %s", sql)
        self.assertIn("ORDER BY retain_until, session_key LIMIT %s", sql)
        self.assertEqual(params, (NOW.replace(tzinfo=None), NOW.replace(tzinfo=None), 20))

    def test_no_early_commit_and_repository_cannot_be_used_after_exit(self):
        with ussd_transaction() as store:
            store.get_credentials(91)
            self.connection.commit.assert_not_called()
            self.assertFalse(hasattr(store, "commit"))
        with self.assertRaises(UssdPersistenceError):
            store.get_credentials(91)

    def test_caught_operation_failure_still_prevents_commit(self):
        self.cursor.execute.side_effect = Error("private data")
        with self.assertRaises(UssdPersistenceError):
            with ussd_transaction() as store:
                try:
                    store.get_credentials(91)
                except UssdPersistenceError:
                    pass
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_trusted_validation_preserves_safe_messages_and_rolls_back(self):
        with self.assertRaises(UssdValidationError) as error:
            with ussd_transaction() as store:
                store.get_credentials("synthetic-sensitive-invalid-id")
        self.assertIsInstance(error.exception, ValueError)
        self.assertEqual(str(error.exception), "A valid farmer ID is required.")
        self.cursor.execute.assert_not_called()
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_caught_trusted_validation_error_invalidates_prior_updates(self):
        with self.assertRaisesRegex(UssdPersistenceError, "^USSD transaction was invalidated\\.$"):
            with ussd_transaction() as store:
                store.set_attempt_state(91, 1, None, NOW)
                with self.assertRaises(UssdValidationError):
                    store.get_credentials(False)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_unexpected_value_errors_and_other_operational_errors_are_sanitized(self):
        targets = (self.helper, self.connection.start_transaction, self.connection.cursor,
                   self.cursor.execute, self.cursor.fetchone, self.connection.commit)
        for exception_type in (ValueError, RuntimeError):
            for target in targets:
                with self.subTest(exception_type=exception_type, target=target):
                    self.connection.reset_mock()
                    target.side_effect = exception_type("synthetic-sensitive-internal-detail")
                    with self.assertNoLogs("app.models.ussd_repository", level="DEBUG"):
                        with self.assertRaises(UssdPersistenceError) as error:
                            with ussd_transaction() as store:
                                store.get_credentials(91)
                    formatted = "".join(traceback.format_exception(
                        type(error.exception), error.exception, error.exception.__traceback__))
                    self.assertNotIn("synthetic-sensitive-internal-detail", str(error.exception) + formatted)
                    self.assertNotIsInstance(error.exception, UssdValidationError)
                    if target is not self.helper:
                        self.connection.rollback.assert_called_once_with()
                        self.connection.close.assert_called_once_with()
                    else:
                        self.connection.rollback.assert_not_called()
                    if target not in (self.helper, self.connection.start_transaction, self.connection.cursor):
                        self.cursor.close.assert_called_once_with()
                    if target is not self.connection.commit:
                        self.connection.commit.assert_not_called()
                    target.side_effect = None
                    self.cursor.fetchone.side_effect = self.fetch

    def test_unexpected_value_error_in_transaction_body_is_sanitized(self):
        with self.assertRaises(UssdPersistenceError) as error:
            with ussd_transaction() as store:
                store.set_attempt_state(91, 1, None, NOW)
                raise ValueError("synthetic-sensitive-transaction-detail")
        self.assertEqual(str(error.exception), "USSD transaction could not be confirmed.")
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_caught_unexpected_value_error_still_prevents_commit(self):
        self.cursor.execute.side_effect = ValueError("synthetic-sensitive-cursor-detail")
        with self.assertRaisesRegex(UssdPersistenceError, "^USSD transaction was invalidated\\.$"):
            with ussd_transaction() as store:
                with self.assertRaises(UssdPersistenceError) as error:
                    store.get_credentials(91)
                self.assertEqual(str(error.exception), "USSD storage operation failed.")
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_connection_start_cursor_query_and_commit_failures_are_safe(self):
        targets = (self.helper, self.connection.start_transaction, self.connection.cursor,
                   self.cursor.execute, self.connection.commit)
        for target in targets:
            with self.subTest(target=target):
                self.connection.reset_mock()
                target.side_effect = Error("private-password-and-sql")
                with self.assertRaises(UssdPersistenceError) as error:
                    with ussd_transaction() as store:
                        store.get_credentials(91)
                self.assertNotIn("private", str(error.exception))
                if target is not self.helper:
                    self.connection.close.assert_called_once_with()
                    self.connection.rollback.assert_called_once_with()
                target.side_effect = None
                self.cursor.fetchone.side_effect = self.fetch

    def test_cleanup_errors_do_not_log_diagnostics_or_prevent_connection_close(self):
        self.cursor.close.side_effect = Error("private-pin-and-phone")
        with self.assertLogs("app.models.ussd_repository", level="WARNING") as logs:
            with ussd_transaction() as store:
                store.get_credentials(91)
        self.assertNotIn("private", str(logs.output))
        self.connection.close.assert_called_once_with()

    def test_rollback_failure_does_not_mask_safe_error_or_cleanup(self):
        self.cursor.execute.side_effect = Error("private query")
        self.connection.rollback.side_effect = Error("private rollback")
        with self.assertLogs("app.models.ussd_repository", level="ERROR") as logs:
            with self.assertRaises(UssdPersistenceError):
                with ussd_transaction() as store:
                    store.get_credentials(91)
        self.assertNotIn("private", str(logs.output))
        self.connection.close.assert_called_once_with()


class UssdSchemaTests(unittest.TestCase):
    def test_migration_matches_fresh_schema_and_does_not_modify_existing_tables(self):
        root = Path(__file__).resolve().parents[1] / "schema"
        migration = (root / "migrations" / "002_ussd_authentication.sql").read_text(encoding="utf-8")
        canonical = (root / "schema.sql").read_text(encoding="utf-8")
        definitions = migration.split("-- USSD TABLE DEFINITIONS: kept identical to the canonical fresh-install schema.\n", 1)[1]
        self.assertEqual(canonical[canonical.index("CREATE TABLE farmer_ussd_credentials"):], definitions)
        self.assertNotRegex(migration, r"(?im)^\s*(ALTER|DROP|DELETE|UPDATE|INSERT)\s")
        self.assertIn("farmer_id BIGINT UNSIGNED NOT NULL", definitions)
        self.assertIn("PRIMARY KEY (session_key, input_count)", definitions)
        self.assertIn("ON DELETE CASCADE", definitions)
        self.assertIn("partial migration state", migration)


if __name__ == "__main__":
    unittest.main()
