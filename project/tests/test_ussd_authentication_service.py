import base64
import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event, RLock, local
from unittest.mock import MagicMock, patch

import dotenv
from flask import Flask

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app.models.ussd_fingerprints import derive_session_key, fingerprint_input, fingerprint_phone
    from app.models.ussd_repository import SAFE_RESPONSES, UssdConflictError, UssdPersistenceError, ussd_transaction
    from app.services.ussd_authentication_service import authenticate_ussd_pin, identify_ussd_candidate
    from app.services.ussd_hash_capacity import scrypt_slot
    from app.services.ussd_pin_service import (
        UssdPinError, hash_ussd_pin, verify_ussd_pin,
    )


NOW = datetime(2026, 11, 4, 9, tzinfo=timezone.utc)
PHONE = "+254712345678"
CODE = "*384*123#"
KEY = b"m" * 32  # Synthetic test material, not deployment keys.


def app_for_tests():
    app = Flask("ussd-authentication-offline-tests")
    app.config.update(
        TESTING=True, SECRET_KEY="synthetic-cookie-key",
        USSD_METADATA_HMAC_KEY=base64.b64encode(KEY).decode("ascii"), USSD_SERVICE_CODE=CODE,
        USSD_PIN_PEPPERS=json.dumps({"1": base64.b64encode(b"p" * 32).decode("ascii")}),
        USSD_PIN_CURRENT_KEY_VERSION="1",
    )
    return app


class FakeStore:
    """Stateful transaction fake, not a substitute for InnoDB integration tests."""

    def __init__(self, credential):
        self.users = {PHONE: {"user_id": 41, "farmer_id": 91, "role": "farmer", "status": "active"}}
        self.credentials = {91: dict(credential, farmer_id=91, pin_version=1, failed_attempts=0, locked_until=None)}
        self.sessions = {}
        self.callbacks = {}
        self.clock = NOW
        self.mutex = RLock()
        self.thread = local()
        self.rollbacks = self.commits = 0
        self.fail_on = None
        self.seed("session")

    def seed(self, session_id, phone=PHONE, *, welcome=False, farmer_id=91):
        key = derive_session_key(KEY, CODE, session_id)
        tag = fingerprint_phone(KEY, phone)
        self.sessions[key] = {
            "session_key": key, "phone_tag": tag, "service_code": CODE,
            "farmer_id": None if welcome else farmer_id, "state": "welcome" if welcome else "pin",
            "verified_pin_version": None, "authenticated_at": None,
            "last_input_count": 0 if welcome else 1,
            "created_at": self.clock.replace(tzinfo=None),
            "expires_at": (self.clock + timedelta(minutes=3)).replace(tzinfo=None),
            "retain_until": (self.clock + timedelta(hours=24)).replace(tzinfo=None),
        }
        for position, text, response in ((0, "", "welcome"), (1, "1", "pin_prompt")):
            if position <= self.sessions[key]["last_input_count"]:
                self.callbacks[key, position] = {
                    "input_fingerprint": fingerprint_input(KEY, key, tag, text),
                    "response_text": SAFE_RESPONSES[response], "response_status": 200,
                }
        return key

    @contextmanager
    def transaction(self):
        with self.mutex:
            saved = copy.deepcopy((self.users, self.credentials, self.sessions, self.callbacks))
            self.thread.in_transaction = True
            try:
                yield self
            except Exception:
                self.users, self.credentials, self.sessions, self.callbacks = saved
                self.rollbacks += 1
                raise
            else:
                self.commits += 1
            finally:
                self.thread.in_transaction = False

    def check(self, operation):
        if self.fail_on == operation:
            raise RuntimeError("synthetic-sensitive-operation-detail")

    def get_farmer_by_phone(self, phone):
        self.check("lookup")
        return copy.deepcopy(self.users.get(phone))

    def get_credentials(self, farmer_id):
        return copy.deepcopy(self.credentials.get(farmer_id))

    def get_session(self, key):
        return copy.deepcopy(self.sessions.get(key))

    def get_callback_result(self, key, position):
        return copy.deepcopy(self.callbacks.get((key, position)))

    def bind_candidate(self, key, farmer_id, now):
        self.sessions[key]["farmer_id"] = farmer_id

    def set_session_state(self, key, state, now):
        self.check("state")
        self.sessions[key].update(state=state, verified_pin_version=None, authenticated_at=None)

    def store_callback_result(self, key, position, fingerprint, code, now):
        self.check("callback")
        assert (key, position) not in self.callbacks
        assert position == self.sessions[key]["last_input_count"] + 1
        self.callbacks[key, position] = {"input_fingerprint": fingerprint,
                                        "response_text": SAFE_RESPONSES[code], "response_status": 200}
        self.sessions[key]["last_input_count"] = position
        return True

    def set_attempt_state(self, farmer_id, attempts, until, now):
        self.check("attempts")
        self.credentials[farmer_id].update(failed_attempts=attempts, locked_until=until)

    def mark_verified(self, key, farmer_id, pin_version, now):
        self.check("verified")
        assert self.credentials[farmer_id]["pin_version"] == pin_version
        record = self.sessions[key]
        if record["state"] != "pin" or self.clock.replace(tzinfo=None) >= record["expires_at"]:
            raise UssdConflictError("USSD verification transition refused.")
        record.update(state="verified", verified_pin_version=pin_version, authenticated_at=self.clock)


class UssdAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            with app_for_tests().app_context():
                cls.credential = hash_ussd_pin(91, "0042")
            guard.assert_not_called()

    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = app_for_tests()
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.db = FakeStore(self.credential)
        self.key = derive_session_key(KEY, CODE, "session")
        tx = patch("app.services.ussd_authentication_service.ussd_transaction", side_effect=lambda: self.db.transaction())
        tx.start()
        self.addCleanup(tx.stop)
        clock = patch("app.services.ussd_authentication_service._now", side_effect=lambda: self.db.clock)
        clock.start()
        self.addCleanup(clock.stop)
        verifier = patch("app.services.ussd_authentication_service.verify_ussd_pin", side_effect=self.verify)
        self.verifier = verifier.start()
        self.addCleanup(verifier.stop)

    def tearDown(self):
        self.guard.assert_not_called()

    def verify(self, farmer_id, pin, encoded, version):
        self.assertFalse(getattr(self.db.thread, "in_transaction", False))
        return pin == "0042"

    def auth(self, text="1*0042", session_id="session", phone=PHONE, code=CODE):
        return authenticate_ussd_pin(phone, session_id, code, text)

    def test_success_with_real_crypto_and_attempt_reset(self):
        self.verifier.side_effect = verify_ussd_pin
        self.db.credentials[91]["failed_attempts"] = 2
        result = self.auth(phone="0712 345 678")
        self.assertTrue(result["authenticated"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.assertEqual(self.db.sessions[self.key]["state"], "verified")
        self.assertEqual(self.db.sessions[self.key]["verified_pin_version"], 1)
        self.assertNotIn("pin_hash", result)
        self.assertNotIn(PHONE, str(result))

    def test_identification_and_duplicate_prompt_do_not_expose_account_existence(self):
        self.db.seed("known", welcome=True)
        unknown_phone = "+254733333333"
        unknown_key = self.db.seed("unknown", unknown_phone, welcome=True)
        known = identify_ussd_candidate("0712345678", "known", CODE)
        unknown = identify_ussd_candidate(unknown_phone, "unknown", CODE)
        self.assertEqual(known, unknown)
        self.assertEqual(known["response"], SAFE_RESPONSES["pin_prompt"])
        self.assertFalse(known["authenticated"])
        self.assertIsNone(self.db.sessions[unknown_key]["farmer_id"])
        duplicate = identify_ussd_candidate(PHONE, "known", CODE)
        self.assertEqual(duplicate, known)
        self.assertFalse(duplicate["authenticated"])
        self.verifier.assert_not_called()

    def test_wrong_pin_ends_dial_and_three_dials_lock_account(self):
        for number in range(1, 4):
            session = "attempt" + str(number)
            key = self.db.seed(session)
            result = self.auth("1*9999", session)
            self.assertFalse(result["authenticated"])
            self.assertEqual(self.db.credentials[91]["failed_attempts"], number)
            self.assertEqual(self.db.sessions[key]["state"], "ended")
        self.assertEqual(self.db.credentials[91]["locked_until"], NOW + timedelta(minutes=15))
        self.db.seed("locked")
        self.verifier.reset_mock()
        self.auth(session_id="locked")
        self.verifier.assert_not_called()
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 3)

    def test_expired_lockout_starts_new_attempt_window_at_fifteen_minutes(self):
        self.db.credentials[91].update(failed_attempts=3, locked_until=NOW + timedelta(minutes=15))
        self.db.clock = NOW + timedelta(minutes=15)
        self.db.seed("after-lock")
        self.auth("1*9999", "after-lock")
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 1)
        self.assertIsNone(self.db.credentials[91]["locked_until"])

    def test_success_after_expired_lockout_resets_state(self):
        self.db.credentials[91].update(failed_attempts=3, locked_until=NOW)
        self.assertTrue(self.auth()["authenticated"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.assertIsNone(self.db.credentials[91]["locked_until"])

    def test_wrong_and_successful_duplicates_never_repeat_hash_or_counter_updates(self):
        first = self.auth("1*9999")
        duplicate = self.auth("1*9999")
        self.assertEqual(first["response"], duplicate["response"])
        self.assertEqual(duplicate, first)
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 1)
        self.verifier.assert_called_once()
        self.db.seed("success")
        self.assertTrue(self.auth(session_id="success")["authenticated"])
        self.verifier.reset_mock()
        replay = self.auth(session_id="success")
        self.assertEqual(replay["response"], SAFE_RESPONSES["pin_verified"])
        self.assertFalse(replay["authenticated"])
        self.verifier.assert_not_called()

    def test_changed_pin_at_already_accepted_position_is_not_another_attempt(self):
        self.auth("1*9999")
        self.verifier.reset_mock()
        result = self.auth("1*0042")
        self.assertFalse(result["authenticated"])
        self.verifier.assert_not_called()
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 1)

    def test_invalid_or_skipped_cumulative_inputs_do_not_count(self):
        for text in ("0042", "1", "1*0042*1", "1*００４２", "1*0042*", "1*123", "1*0042\n"):
            self.assertFalse(self.auth(text)["authenticated"])
        self.db.sessions[self.key]["last_input_count"] = 0
        self.assertFalse(self.auth()["authenticated"])
        self.verifier.assert_not_called()
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)

    def test_missing_or_changed_prefix_refuses_hashing(self):
        self.db.callbacks[self.key, 1]["input_fingerprint"] = b"x" * 32
        self.auth()
        del self.db.callbacks[self.key, 1]
        self.auth()
        self.verifier.assert_not_called()

    def test_expired_ended_and_wrongly_bound_sessions_are_refused(self):
        original = copy.deepcopy(self.db.sessions[self.key])
        for changes in ({"state": "ended"}, {"expires_at": NOW.replace(tzinfo=None)},
                        {"farmer_id": 92}, {"phone_tag": b"x" * 32}, {"service_code": "*123#"},
                        {"verified_pin_version": 1}, {"created_at": (NOW + timedelta(seconds=1)).replace(tzinfo=None)}):
            self.db.sessions[self.key] = dict(original, **changes)
            self.assertFalse(self.auth()["authenticated"])
        self.verifier.assert_not_called()

    def test_inactive_wrong_role_unregistered_and_missing_profiles_share_failure(self):
        expected = self.auth("bad-input")
        original = dict(self.db.users[PHONE])
        for user in (None, dict(original, status="disabled"), dict(original, status="pending"),
                     dict(original, role="officer"), dict(original, farmer_id=None)):
            self.db.users[PHONE] = user
            self.assertEqual(self.auth(), expected)
        self.verifier.assert_not_called()
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)

    def test_missing_and_corrupt_credentials_never_count_incorrect_attempts(self):
        original = dict(self.db.credentials[91])
        cases = (None, dict(original, pin_hash=None), dict(original, pin_hash="corrupted"),
                 dict(original, pin_key_version="1"), dict(original, pin_version=0),
                 dict(original, failed_attempts=True), dict(original, failed_attempts=4),
                 dict(original, failed_attempts=3), dict(original, locked_until="bad-date"))
        for credential in cases:
            self.db.credentials[91] = copy.deepcopy(credential)
            self.assertFalse(self.auth()["authenticated"])
            self.assertEqual(self.db.credentials[91], credential)
        self.verifier.assert_not_called()

    def test_failed_duplicates_do_not_add_account_existence_information(self):
        known = self.auth("1*9999")
        known_duplicate = self.auth("1*9999")
        self.db.users[PHONE] = None
        unknown = self.auth("1*9999")
        unknown_duplicate = self.auth("1*9999")
        self.assertEqual(known, known_duplicate)
        self.assertEqual(known, unknown)
        self.assertEqual(unknown, unknown_duplicate)
        self.assertEqual(set(known), {"authenticated", "response"})

    def test_missing_pepper_and_bad_metadata_configuration_do_not_mutate_state(self):
        for changes in ({"USSD_PIN_PEPPERS": None}, {"USSD_PIN_PEPPERS": "{}"},
                        {"USSD_METADATA_HMAC_KEY": "bad"}, {"USSD_SERVICE_CODE": "*123#"}):
            before = copy.deepcopy(self.app.config)
            self.app.config.update(changes)
            self.assertFalse(self.auth()["authenticated"])
            self.app.config.clear()
            self.app.config.update(before)
        self.db.credentials[91]["pin_key_version"] = 2
        self.auth()
        self.verifier.assert_not_called()
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)

    def test_scrypt_runs_outside_database_transaction(self):
        self.assertTrue(self.auth()["authenticated"])
        self.verifier.assert_called_once()
        self.assertEqual(self.db.commits, 2)

    def test_reset_or_credential_change_during_hash_refuses_stale_result(self):
        for change in ("version", "hash", "pepper", "ended"):
            self.db = FakeStore(self.credential)
            def verify(*args):
                if change == "version":
                    self.db.credentials[91]["pin_version"] += 1
                elif change == "hash":
                    value = self.db.credentials[91]["pin_hash"]
                    self.db.credentials[91]["pin_hash"] = value[:-1] + ("a" if value[-1] != "a" else "b")
                elif change == "pepper":
                    self.db.credentials[91]["pin_key_version"] = 2
                else:
                    self.db.sessions[self.key]["state"] = "ended"
                return True
            self.verifier.side_effect = verify
            self.assertFalse(self.auth()["authenticated"])
            self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
            self.assertNotIn((self.key, 2), self.db.callbacks)

    def test_account_deactivation_phone_reassignment_or_expiry_during_hash_refuses_commit(self):
        for change in ("disabled", "phone-owner", "expired"):
            self.db = FakeStore(self.credential)
            def verify(*args):
                if change == "disabled":
                    self.db.users[PHONE]["status"] = "disabled"
                elif change == "phone-owner":
                    self.db.users[PHONE]["user_id"] = 42
                else:
                    self.db.clock += timedelta(minutes=3)
                return True
            self.verifier.side_effect = verify
            self.assertFalse(self.auth()["authenticated"])
            self.assertNotEqual(self.db.sessions[self.key]["state"], "verified")
            self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)

    def test_session_lifetime_replacement_during_hash_refuses_stale_result(self):
        def verify(*args):
            self.db.sessions[self.key]["expires_at"] += timedelta(seconds=1)
            return True
        self.verifier.side_effect = verify
        self.assertFalse(self.auth()["authenticated"])
        self.assertNotIn((self.key, 2), self.db.callbacks)

    def delay_second_phase_prefix(self, seconds):
        original = self.db.get_callback_result
        def read(key, position):
            result = original(key, position)
            if self.db.commits == 1 and position == 1:
                self.db.clock += timedelta(seconds=seconds)
            return result
        return patch.object(self.db, "get_callback_result", side_effect=read)

    def test_expiry_during_phase_two_reads_refuses_all_writes(self):
        before = copy.deepcopy((self.db.credentials, self.db.sessions, self.db.callbacks))
        with self.delay_second_phase_prefix(180):
            result = self.auth()
        self.assertFalse(result["authenticated"])
        self.assertEqual((self.db.credentials, self.db.sessions, self.db.callbacks), before)
        self.assertEqual(self.db.rollbacks, 1)

    def test_third_failure_deadline_uses_fresh_time_after_phase_two_reads(self):
        self.db.credentials[91]["failed_attempts"] = 2
        with self.delay_second_phase_prefix(10):
            self.assertFalse(self.auth("1*9999")["authenticated"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 3)
        self.assertEqual(self.db.credentials[91]["locked_until"], NOW + timedelta(minutes=15, seconds=10))

    def test_mixed_concurrent_attempts_at_third_failure_boundary(self):
        # Model both write orders with overlapping hash phases. The global
        # crypto gate itself is tested separately; workers/processes may overlap.
        for first_success in (False, True):
            with self.subTest(first_success=first_success):
                self.db = FakeStore(self.credential)
                self.db.credentials[91]["failed_attempts"] = 2
                good_key = self.db.seed("good")
                bad_key = self.db.seed("bad")
                barrier, release_second = Barrier(2), Event()
                def verify(farmer_id, pin, encoded, version):
                    self.assertFalse(getattr(self.db.thread, "in_transaction", False))
                    barrier.wait(timeout=5)
                    matched = pin == "0042"
                    if matched != first_success and not release_second.wait(timeout=5):
                        raise RuntimeError("synthetic test timeout")
                    return matched
                self.verifier.side_effect = verify
                def attempt(matched):
                    with self.app.app_context():
                        return self.auth("1*0042" if matched else "1*9999", "good" if matched else "bad")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    good = pool.submit(attempt, True)
                    bad = pool.submit(attempt, False)
                    try:
                        (good if first_success else bad).result(timeout=5)
                    finally:
                        release_second.set()
                    good_result, bad_result = good.result(timeout=5), bad.result(timeout=5)
                self.assertFalse(bad_result["authenticated"])
                self.assertEqual(good_result["authenticated"], first_success)
                self.assertEqual(self.db.credentials[91]["failed_attempts"], 1 if first_success else 3)
                self.assertEqual(self.db.credentials[91]["locked_until"],
                                 None if first_success else NOW + timedelta(minutes=15))
                self.assertEqual(self.db.sessions[bad_key]["state"], "ended")
                self.assertEqual(self.db.sessions[good_key]["state"], "verified" if first_success else "pin")
                self.assertEqual((good_key, 2) in self.db.callbacks, first_success)

    def test_verified_session_replay_after_pin_reset_is_not_authorized(self):
        self.assertTrue(self.auth()["authenticated"])
        self.db.credentials[91]["pin_version"] += 1
        self.db.sessions[self.key].update(state="ended", verified_pin_version=None, authenticated_at=None)
        self.verifier.reset_mock()
        self.assertFalse(self.auth()["authenticated"])
        self.verifier.assert_not_called()

    def test_failure_after_updates_rolls_back_credentials_session_and_callback(self):
        for operation in ("attempts", "callback", "verified"):
            self.db = FakeStore(self.credential)
            self.db.credentials[91]["failed_attempts"] = 2
            self.db.fail_on = operation
            with self.assertNoLogs(level="DEBUG"):
                result = self.auth()
            self.assertFalse(result["authenticated"])
            self.assertEqual(self.db.credentials[91]["failed_attempts"], 2)
            self.assertEqual(self.db.sessions[self.key]["state"], "pin")
            self.assertNotIn((self.key, 2), self.db.callbacks)
            self.assertEqual(self.db.rollbacks, 1)

    def test_crypto_failure_does_not_commit_attempt_or_session_state(self):
        self.verifier.side_effect = UssdPinError("synthetic-configuration-error")
        self.assertFalse(self.auth()["authenticated"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.assertEqual(self.db.sessions[self.key]["state"], "pin")
        self.assertNotIn((self.key, 2), self.db.callbacks)
        self.assertEqual(self.db.commits, 1)  # Read-only snapshot only.

    def test_hash_capacity_exhaustion_is_non_waiting_and_does_not_count(self):
        self.verifier.side_effect = verify_ussd_pin
        with scrypt_slot():
            result = self.auth()
        self.assertFalse(result["authenticated"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.assertNotIn((self.key, 2), self.db.callbacks)
        self.assertTrue(self.auth()["authenticated"])  # Slot was not leaked.

    def test_both_public_crypto_entry_points_share_capacity_and_release_after_errors(self):
        with scrypt_slot():
            with self.assertRaises(UssdPinError):
                hash_ussd_pin(91, "0042")
            with self.assertRaises(UssdPinError):
                verify_ussd_pin(91, "0042", self.credential["pin_hash"], 1)
        with patch("app.services.ussd_pin_service.check_password_hash", side_effect=MemoryError("synthetic")):
            with self.assertRaises(UssdPinError):
                verify_ussd_pin(91, "0042", self.credential["pin_hash"], 1)
        with scrypt_slot():
            pass

    def test_scrypt_capacity_is_shared_across_threads_and_apps_without_waiting(self):
        started, release = Event(), Event()
        def generate(*args, **kwargs):
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("synthetic test timeout")
            return self.credential["pin_hash"]
        def hash_in_worker():
            with app_for_tests().app_context():
                return hash_ussd_pin(91, "0042")
        with patch("app.services.ussd_pin_service.generate_password_hash", side_effect=generate):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(hash_in_worker)
                try:
                    self.assertTrue(started.wait(timeout=5))
                    with self.assertRaises(UssdPinError):
                        verify_ussd_pin(91, "0042", self.credential["pin_hash"], 1)
                finally:
                    release.set()
                self.assertEqual(future.result(timeout=5)["pin_key_version"], 1)

    def test_concurrent_attempts_use_current_counter_and_cannot_bypass_third_failure(self):
        for index in range(3):
            self.db.seed("parallel" + str(index))
        barrier = Barrier(3)
        def verify(*args):
            barrier.wait(timeout=5)
            return False
        self.verifier.side_effect = verify
        def attempt(index):
            with self.app.app_context():
                return self.auth("1*9999", "parallel" + str(index))
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(attempt, range(3)))
        self.assertTrue(all(not result["authenticated"] for result in results))
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 3)
        self.assertEqual(self.db.credentials[91]["locked_until"], NOW + timedelta(minutes=15))

    def test_concurrent_duplicate_counts_once(self):
        barrier = Barrier(2)
        def verify(*args):
            barrier.wait(timeout=5)
            return False
        self.verifier.side_effect = verify
        def attempt(_):
            with self.app.app_context():
                return self.auth("1*9999")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 1)
        self.assertEqual(results[0], results[1])
        self.assertIn((self.key, 2), self.db.callbacks)

    def test_new_lockout_while_hashing_blocks_even_a_correct_pin(self):
        def verify(*args):
            self.db.credentials[91].update(failed_attempts=3, locked_until=NOW + timedelta(minutes=15))
            return True
        self.verifier.side_effect = verify
        self.assertFalse(self.auth()["authenticated"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 3)
        self.assertEqual(self.db.sessions[self.key]["state"], "pin")

    def test_identification_refuses_rebinding_and_missing_welcome_prefix(self):
        self.db.seed("welcome", welcome=True)
        key = derive_session_key(KEY, CODE, "welcome")
        self.db.sessions[key]["farmer_id"] = 92
        self.assertEqual(identify_ussd_candidate(PHONE, "welcome", CODE)["response"], SAFE_RESPONSES["verification_failed"])
        self.db.sessions[key]["farmer_id"] = None
        del self.db.callbacks[key, 0]
        self.assertEqual(identify_ussd_candidate(PHONE, "welcome", CODE)["response"], SAFE_RESPONSES["verification_failed"])


class MockAuthenticationConnector:
    """Stateful connector boundary for the real repository/transaction manager.

    This models commit/rollback and UTC statement time, not a SQL engine or
    InnoDB locking. SQL predicates are independently asserted below and in the
    persistence tests.
    """

    def __init__(self, data):
        self.data = data
        self.connections = []
        self.queries = []
        self.before_transition = lambda: None
        self.force_zero = False

    def connect(self):
        connection, cursor = MagicMock(), MagicMock()
        connection.cursor.return_value = cursor
        self.connections.append(connection)
        saved = copy.deepcopy((self.data.credentials, self.data.sessions, self.data.callbacks))
        def rollback():
            self.data.credentials, self.data.sessions, self.data.callbacks = copy.deepcopy(saved)
        connection.rollback.side_effect = rollback
        def execute(sql, params):
            self.queries.append((sql, params))
            cursor.rowcount = 1
            row = None
            if sql.startswith("SELECT"):
                if "FROM users" in sql:
                    row = self.data.users.get(params[0])
                elif "FROM farmers" in sql:
                    row = next((item for item in self.data.users.values() if item["user_id"] == params[0]), None)
                elif "FROM farmer_ussd_credentials" in sql:
                    row = self.data.credentials.get(params[0])
                elif "FROM ussd_sessions" in sql:
                    row = self.data.sessions.get(params[0])
                elif "FROM ussd_callback_results" in sql:
                    row = self.data.callbacks.get(params)
                else:
                    raise AssertionError("Unexpected mocked SELECT")
                cursor.fetchone.return_value = copy.deepcopy(row)
            elif sql.startswith("UPDATE farmer_ussd_credentials SET failed_attempts"):
                attempts, until, stamp, farmer_id = params
                self.data.credentials[farmer_id].update(failed_attempts=attempts, locked_until=until, updated_at=stamp)
            elif sql.startswith("INSERT INTO ussd_callback_results"):
                key, position, fingerprint, text, status, stamp = params
                self.data.callbacks[key, position] = dict(input_fingerprint=fingerprint,
                                                         response_text=text, response_status=status, created_at=stamp)
            elif sql.startswith("UPDATE ussd_sessions SET last_input_count"):
                self.data.sessions[params[1]]["last_input_count"] = params[0]
            elif sql.startswith("UPDATE ussd_sessions SET state = 'verified'"):
                self.before_transition()
                version, key, farmer_id, credential_id, expected_version = params
                session = self.data.sessions[key]
                credential = self.data.credentials[credential_id]
                stamp = self.data.clock.replace(tzinfo=None)
                until = credential["locked_until"]
                if until is not None and until.tzinfo is not None:
                    until = until.astimezone(timezone.utc).replace(tzinfo=None)
                valid = (not self.force_zero and session["state"] == "pin"
                         and session["farmer_id"] == farmer_id
                         and session["verified_pin_version"] is None and session["authenticated_at"] is None
                         and session["created_at"] <= stamp < session["expires_at"]
                         and credential["pin_version"] == expected_version and credential["pin_hash"] is not None
                         and (until is None or until <= stamp))
                cursor.rowcount = int(valid)
                if valid:
                    session.update(state="verified", verified_pin_version=version, authenticated_at=stamp)
            else:
                raise AssertionError("Unexpected mocked SQL mutation")
        cursor.execute.side_effect = execute
        return connection


class UssdAuthenticationTransitionTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        context = app_for_tests().app_context()
        context.push()
        self.addCleanup(context.pop)
        # Shape-valid fixture; crypto is tested separately and not performed here.
        self.db = FakeStore({"pin_hash": "scrypt:131072:8:1$" + "s" * 32 + "$" + "a" * 128,
                             "pin_key_version": 1})
        self.db.credentials[91]["failed_attempts"] = 2
        self.key = derive_session_key(KEY, CODE, "session")
        self.connector = MockAuthenticationConnector(self.db)
        helper = patch("app.models.ussd_repository.get_db_connection", side_effect=self.connector.connect)
        helper.start()
        self.addCleanup(helper.stop)
        # Deliberately stale application clock: only the SQL guard can detect a
        # later database-time expiry. The service and real repository both run.
        clock = patch("app.services.ussd_authentication_service._now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        verifier = patch("app.services.ussd_authentication_service.verify_ussd_pin", return_value=True)
        verifier.start()
        self.addCleanup(verifier.stop)

    def tearDown(self):
        self.guard.assert_not_called()

    def authenticate(self):
        return authenticate_ussd_pin(PHONE, "session", CODE, "1*0042")

    def assert_rolled_back(self, before):
        self.assertEqual((self.db.credentials, self.db.sessions, self.db.callbacks), before)
        read, write = self.connector.connections
        read.commit.assert_called_once_with()
        write.commit.assert_not_called()
        write.rollback.assert_called_once_with()
        for connection in (read, write):
            connection.close.assert_called_once_with()
            connection.cursor.return_value.close.assert_called_once_with()

    def test_expiry_immediately_before_conditional_transition_rolls_back_all_changes(self):
        before = copy.deepcopy((self.db.credentials, self.db.sessions, self.db.callbacks))
        def expire():
            # Confirm successful effects were attempted before the final guard.
            self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
            self.assertIn((self.key, 2), self.db.callbacks)
            self.assertEqual(self.db.sessions[self.key]["last_input_count"], 2)
            self.db.clock = NOW + timedelta(minutes=3)
        self.connector.before_transition = expire
        result = self.authenticate()
        self.assertFalse(result["authenticated"])
        self.assertEqual(result["response"], SAFE_RESPONSES["verification_failed"])
        self.assert_rolled_back(before)

    def test_zero_row_conditional_update_rolls_back_and_never_authenticates(self):
        before = copy.deepcopy((self.db.credentials, self.db.sessions, self.db.callbacks))
        self.connector.force_zero = True
        self.assertFalse(self.authenticate()["authenticated"])
        self.assert_rolled_back(before)

    def test_valid_session_through_transition_commits_database_timestamp(self):
        transition_time = NOW + timedelta(minutes=3) - timedelta(microseconds=1)
        self.connector.before_transition = lambda: setattr(self.db, "clock", transition_time)
        self.assertTrue(self.authenticate()["authenticated"])
        self.assertEqual(self.db.sessions[self.key]["authenticated_at"], transition_time.replace(tzinfo=None))
        self.assertEqual(self.db.sessions[self.key]["state"], "verified")
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.assertIn((self.key, 2), self.db.callbacks)
        final_sql, params = self.connector.queries[-1]
        self.assertIn("expires_at > UTC_TIMESTAMP(6)", final_sql)
        self.assertIn("authenticated_at = UTC_TIMESTAMP(6)", final_sql)
        self.assertNotIn(NOW, params)
        for connection in self.connector.connections:
            connection.commit.assert_called_once_with()
            connection.rollback.assert_not_called()
            connection.close.assert_called_once_with()
            connection.cursor.return_value.close.assert_called_once_with()


class UssdRepositoryLookupTests(unittest.TestCase):
    def test_unknown_and_invalid_phones_never_fetch_web_credentials(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = None
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            with patch("app.models.ussd_repository.get_db_connection", return_value=connection):
                with ussd_transaction() as store:
                    self.assertIsNone(store.get_farmer_by_phone(PHONE))
                self.assertEqual(cursor.execute.call_count, 1)
                cursor.reset_mock()
                with self.assertRaises(ValueError):
                    with ussd_transaction() as store:
                        store.get_farmer_by_phone("0712345678")
                cursor.execute.assert_not_called()
            guard.assert_not_called()

    def test_lookup_operational_failure_rolls_back_and_closes_resources(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.execute.side_effect = ValueError("synthetic-sensitive-driver-detail")
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            with patch("app.models.ussd_repository.get_db_connection", return_value=connection):
                with self.assertRaises(UssdPersistenceError) as error:
                    with ussd_transaction() as store:
                        store.get_farmer_by_phone(PHONE)
            guard.assert_not_called()
        self.assertNotIn("synthetic-sensitive", str(error.exception))
        connection.rollback.assert_called_once_with()
        connection.commit.assert_not_called()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_parameterized_phone_lookup_locks_user_before_profile_without_web_hash(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.side_effect = [
            {"user_id": 41, "role": "farmer", "status": "active"}, {"farmer_id": 91, "user_id": 41},
        ]
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            with patch("app.models.ussd_repository.get_db_connection", return_value=connection):
                with ussd_transaction() as store:
                    row = store.get_farmer_by_phone(PHONE)
        self.assertEqual(row["farmer_id"], 91)
        calls = cursor.execute.call_args_list
        self.assertEqual(calls[0].args[1], (PHONE,))
        self.assertIn("FROM users", calls[0].args[0])
        self.assertIn("FROM farmers", calls[1].args[0])
        self.assertTrue(all("FOR UPDATE" in call.args[0] for call in calls))
        self.assertNotIn("password_hash", str(calls))
        connection.commit.assert_called_once_with()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()
        guard.assert_not_called()

    def test_guarded_binding_rejects_ended_and_other_farmer_and_rolls_back(self):
        for state, farmer in (("ended", None), ("welcome", 92)):
            connection = MagicMock()
            cursor = connection.cursor.return_value
            cursor.fetchone.side_effect = [None, {"state": state, "farmer_id": farmer,
                                                 "expires_at": NOW + timedelta(minutes=3)}]
            with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
                with patch("app.models.ussd_repository.get_db_connection", return_value=connection):
                    with self.assertRaises(UssdPersistenceError):
                        with ussd_transaction() as store:
                            store.bind_candidate(b"s" * 32, 91, NOW)
                guard.assert_not_called()
            connection.rollback.assert_called_once_with()
            connection.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
