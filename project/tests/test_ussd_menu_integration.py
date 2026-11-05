"""Flask -> ingress -> orchestrator -> real auth/repository, mocked connector.

The fake serializes transactions and models rollback; no real SQL, network, TLS,
distributed limiter or InnoDB concurrency guarantees are established here.
"""

import base64
import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, RLock, local
from unittest.mock import patch

import dotenv

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.models.ussd_repository import SAFE_RESPONSES
    from app.models.ussd_fingerprints import derive_session_key
    from app.services.ussd_authentication_service import authenticate_ussd_pin, advance_ussd_menu
    from app.services.ussd_hash_capacity import scrypt_slot
    from app.services.ussd_pin_service import hash_ussd_pin, verify_ussd_pin, UssdPinError

from test_ussd_callback_foundation import OfflineConnector, OfflineSharedLimiter, NOW, PHONE, CODE, RELAY, METADATA
from test_ussd_authentication_service import app_for_tests


class MenuConnector(OfflineConnector):
    def __init__(self, credential):
        super().__init__()
        self.users = {PHONE: dict(user_id=41, farmer_id=91, role="farmer", status="active")}
        self.credentials = {91: dict(credential, farmer_id=91, pin_version=1, failed_attempts=0, locked_until=None)}
        self.mutex, self.thread = RLock(), local()
        self.current_time = lambda: NOW
        self.force_zero = False

    def connect(self):
        self.mutex.acquire()
        connection = super().connect()
        self.thread.in_transaction = True
        cursor = connection.cursor.return_value
        base_execute = cursor.execute.side_effect
        base_rollback = connection.rollback.side_effect
        credentials = copy.deepcopy(self.credentials)
        def rollback():
            base_rollback()
            self.credentials = copy.deepcopy(credentials)
        connection.rollback.side_effect = rollback
        def close():
            self.thread.in_transaction = False
            self.mutex.release()
        connection.close.side_effect = close
        def execute(sql, params):
            if sql.startswith("UPDATE ussd_sessions SET last_input_count"):
                return base_execute(sql, params)
            extra = ("FROM users" in sql or "FROM farmers" in sql or "FROM farmer_ussd_credentials" in sql
                     or sql.startswith("UPDATE farmer_ussd_credentials")
                     or sql.startswith("UPDATE ussd_sessions SET farmer_id")
                     or sql.startswith("UPDATE ussd_sessions SET state = 'verified'"))
            if not extra:
                return base_execute(sql, params)
            self.queries.append((sql, params))
            if self.fail_at and self.fail_at in sql:
                raise RuntimeError("synthetic-sensitive-driver-detail")
            cursor.rowcount = 1
            if sql.startswith("SELECT"):
                if "FROM users" in sql:
                    row = self.users.get(params[0])
                elif "FROM farmers" in sql:
                    row = next((row for row in self.users.values() if row["user_id"] == params[0]), None)
                else:
                    row = self.credentials.get(params[0])
                cursor.fetchone.return_value = copy.deepcopy(row)
            elif sql.startswith("UPDATE farmer_ussd_credentials"):
                attempts, until, stamp, farmer_id = params
                self.credentials[farmer_id].update(failed_attempts=attempts, locked_until=until, updated_at=stamp)
            elif sql.startswith("UPDATE ussd_sessions SET farmer_id"):
                self.sessions[params[1]]["farmer_id"] = params[0]
            else:
                version, key, farmer_id, credential_id, expected_version = params
                row, credential = self.sessions[key], self.credentials[credential_id]
                now = self.current_time().replace(tzinfo=None)
                valid = (not self.force_zero and row["state"] == "pin" and row["farmer_id"] == farmer_id
                         and row["verified_pin_version"] is None and row["authenticated_at"] is None
                         and row["created_at"] <= now < row["expires_at"]
                         and credential["pin_version"] == expected_version and credential["pin_hash"] is not None
                         and (credential["locked_until"] is None or credential["locked_until"] <= now))
                cursor.rowcount = int(valid)
                if valid:
                    row.update(state="verified", verified_pin_version=version, authenticated_at=now)
            self.after_execute(sql)
        cursor.execute.side_effect = execute
        return connection


class UssdMenuIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            with app_for_tests().app_context():
                cls.credential = hash_ussd_pin(91, "0042")
                cls.zero_credential = hash_ussd_pin(91, "0000")
            guard.assert_not_called()

    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(
            TESTING=True, DEBUG=False, SECRET_KEY="synthetic-menu-cookie-key",
            USSD_PERSISTENT_FLOW_ENABLED=True, USSD_RELAY_SECRET=RELAY,
            USSD_TRUSTED_RELAY_IPS="10.0.0.8", USSD_METADATA_HMAC_KEY=METADATA, USSD_SERVICE_CODE=CODE,
            USSD_PIN_PEPPERS=json.dumps({"1": base64.b64encode(b"p" * 32).decode("ascii")}),
            USSD_PIN_CURRENT_KEY_VERSION="1", AFRICASTALKING_API_KEY=None,
            USSD_PHONE_CALLBACKS_PER_MINUTE=20, USSD_PHONE_CALLBACK_BURST=5, USSD_NEW_SESSIONS_PER_MINUTE=3,
            USSD_GLOBAL_CALLBACKS_PER_SECOND=10, USSD_GLOBAL_CALLBACK_BURST=10,
            USSD_SESSION_SECONDS=180, USSD_RETENTION_SECONDS=86400,
        )
        self.limiter = OfflineSharedLimiter()
        self.app.extensions["ussd_shared_limiter"] = self.limiter
        self.db, self.clock = MenuConnector(self.credential), NOW
        self.db.current_time = lambda: self.clock
        helper = patch("app.models.ussd_repository.get_db_connection", side_effect=self.db.connect)
        helper.start()
        self.addCleanup(helper.stop)
        for module in ("ussd_callback_service", "ussd_authentication_service"):
            clock = patch("app.services." + module + "._now", side_effect=lambda: self.clock)
            clock.start()
            self.addCleanup(clock.stop)
        verifier = patch("app.services.ussd_authentication_service.verify_ussd_pin", side_effect=self.verify)
        self.verifier = verifier.start()
        self.addCleanup(verifier.stop)
        self.client = self.app.test_client()

    def tearDown(self):
        self.guard.assert_not_called()
        for connection in self.db.connections:
            connection.close.assert_called_once_with()
            connection.cursor.return_value.close.assert_called_once_with()
        for sql, params in self.db.queries:
            self.assertEqual(sql.count("%s"), len(params))
            self.assertNotIn("password_hash", sql)

    def verify(self, farmer_id, pin, encoded, version):
        self.assertFalse(getattr(self.db.thread, "in_transaction", False))
        return pin == "0042"

    def post(self, text="", sid="menu-session", phone=PHONE, client=None):
        response = (client or self.client).post("/ussd", base_url="https://localhost", data={
            "sessionId": sid, "serviceCode": CODE, "phoneNumber": phone, "text": text,
        }, headers={"X-FarmAdvis-USSD-Relay": RELAY}, environ_overrides={"REMOTE_ADDR": "10.0.0.8"})
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", response.headers)
        return response.get_data(as_text=True)

    def prompt(self, sid="menu-session", phone=PHONE):
        self.assertEqual(self.post(sid=sid, phone=phone), SAFE_RESPONSES["welcome"])
        self.assertEqual(self.post("1", sid, phone), SAFE_RESPONSES["pin_prompt"])
        return derive_session_key(base64.b64decode(METADATA), CODE, sid)

    def test_full_flow_persists_identical_success_menu_and_exits(self):
        key = self.prompt()
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["pin_verified"])
        self.assertIn("0. Exit", self.db.callbacks[key, 2]["response_text"])
        self.assertEqual(self.db.callbacks[key, 2]["response_text"], self.post("1*0042"))
        self.verifier.assert_called_once()
        self.assertEqual(self.post("1*0042*1"), SAFE_RESPONSES["pin_verified"])
        self.assertEqual(self.post("1*0042*1*0"), SAFE_RESPONSES["exit"])
        self.assertEqual(self.db.sessions[key]["state"], "ended")
        self.assertIsNone(self.db.sessions[key]["authenticated_at"])
        self.assertEqual(self.post("1*0042*1*0"), SAFE_RESPONSES["exit"])
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.post("1"), SAFE_RESPONSES["verification_failed"])
        self.verifier.assert_called_once()

    def test_real_crypto_preserves_leading_and_all_zero_pins(self):
        self.verifier.side_effect = verify_ussd_pin
        self.prompt()
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["pin_verified"])
        self.db.credentials[91].update(self.zero_credential, pin_version=2)
        self.prompt("all-zero")
        self.assertEqual(self.post("1*0000", "all-zero"), SAFE_RESPONSES["pin_verified"])
        self.assertEqual(self.post("1*0000*0", "all-zero"), SAFE_RESPONSES["exit"])

    def test_wrong_pin_duplicates_lockout_across_dials_and_expiry(self):
        for index in range(3):
            sid = "wrong" + str(index)
            self.prompt(sid)
            self.assertEqual(self.post("1*9999", sid), SAFE_RESPONSES["verification_failed"])
            self.post("1*9999", sid)
            self.assertEqual(self.db.credentials[91]["failed_attempts"], index + 1)
        self.assertEqual(self.verifier.call_count, 3)
        self.assertEqual(self.db.credentials[91]["locked_until"], (NOW + timedelta(minutes=15)).replace(tzinfo=None))
        self.prompt("locked")
        self.post("1*0042", "locked")
        self.assertEqual(self.verifier.call_count, 3)
        self.clock += timedelta(minutes=15)
        self.prompt("unlocked")
        self.assertEqual(self.post("1*0042", "unlocked"), SAFE_RESPONSES["pin_verified"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.assertIsNone(self.db.credentials[91]["locked_until"])

    def test_cancel_known_unknown_and_unenrolled_accounts_without_hashing(self):
        for sid, phone, missing in (("known", PHONE, False), ("unknown", "+254733333333", False),
                                    ("unenrolled", PHONE, True)):
            if missing:
                self.db.credentials[91]["pin_hash"] = None
            self.prompt(sid, phone)
            self.assertEqual(self.post("1*0", sid, phone), SAFE_RESPONSES["exit"])
            self.assertEqual(self.post("1*0", sid, phone), SAFE_RESPONSES["exit"])
        self.verifier.assert_not_called()

    def test_unknown_missing_corrupt_or_missing_pepper_have_same_failure_without_attempts(self):
        original = copy.deepcopy(self.db.credentials[91])
        cases = (("unknown", "+254733333333", None), ("missing", PHONE, None), ("missing-row", PHONE, None),
                 ("corrupt", PHONE, "corrupted"), ("pepper", PHONE, original["pin_hash"]))
        for sid, phone, encoded in cases:
            self.db.credentials[91] = dict(original, pin_hash=encoded)
            if sid == "pepper":
                self.db.credentials[91]["pin_key_version"] = 2
            if sid == "missing-row":
                del self.db.credentials[91]
            self.prompt(sid, phone)
            self.assertEqual(self.post("1*0042", sid, phone), SAFE_RESPONSES["verification_failed"])
            if sid != "missing-row":
                self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.verifier.assert_not_called()

    def test_no_missing_history_or_conflicting_callback_can_authorize(self):
        self.assertEqual(self.post("1"), SAFE_RESPONSES["verification_failed"])
        key = self.prompt()
        self.assertEqual(self.post("1*0042*0"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["pin_verified"])
        self.assertEqual(self.post("1*9999"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.post("1*9999*0"), SAFE_RESPONSES["verification_failed"])
        del self.db.callbacks[key, 1]
        self.assertEqual(self.post("1*0042*0"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.db.sessions[key]["state"], "verified")
        self.verifier.assert_called_once()

    def test_expired_and_wrong_phone_callbacks_do_not_authenticate_or_exit(self):
        key = self.prompt()
        self.assertEqual(self.post("1*0042", phone="+254733333333"), SAFE_RESPONSES["verification_failed"])
        self.clock += timedelta(seconds=180)
        for text in ("1", "1*0042", "1*0"):
            self.assertEqual(self.post(text), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.db.sessions[key]["state"], "pin")
        self.verifier.assert_not_called()

    def test_followups_recheck_pin_version_account_status_lockout_and_expiry(self):
        key = self.prompt()
        self.post("1*0042")
        original = copy.deepcopy(self.db.credentials[91])
        for change in ("version", "disabled", "locked", "expired"):
            self.db.credentials[91] = copy.deepcopy(original)
            self.db.users[PHONE]["status"] = "active"
            self.clock = NOW
            if change == "version":
                self.db.credentials[91]["pin_version"] += 1
            elif change == "disabled":
                self.db.users[PHONE]["status"] = "disabled"
            elif change == "locked":
                self.db.credentials[91].update(failed_attempts=3, locked_until=NOW.replace(tzinfo=None) + timedelta(minutes=15))
            else:
                self.clock += timedelta(seconds=180)
            self.assertEqual(self.post("1*0042*1"), SAFE_RESPONSES["verification_failed"])
            self.assertNotIn((key, 3), self.db.callbacks)

    def test_cached_followup_success_is_revoked_by_pin_reset_or_lockout(self):
        self.prompt()
        self.post("1*0042")
        self.assertEqual(self.post("1*0042*1"), SAFE_RESPONSES["pin_verified"])
        original = copy.deepcopy(self.db.credentials[91])
        for change in ("version", "locked"):
            self.db.credentials[91] = copy.deepcopy(original)
            if change == "version":
                self.db.credentials[91]["pin_version"] += 1
            else:
                self.db.credentials[91].update(failed_attempts=3,
                                               locked_until=(NOW + timedelta(minutes=15)).replace(tzinfo=None))
            before = copy.deepcopy((self.db.sessions, self.db.callbacks, self.db.credentials))
            self.assertEqual(self.post("1*0042*1"), SAFE_RESPONSES["verification_failed"])
            self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
        self.verifier.assert_called_once()

    def test_identification_error_and_invalid_verified_selection_do_not_leak_or_reset(self):
        self.post()
        self.db.fail_at = "FROM users"
        self.assertEqual(self.post("1"), SAFE_RESPONSES["verification_failed"])
        self.assertTrue(all(position == 0 for _, position in self.db.callbacks))
        self.db.fail_at = None
        self.post("1")
        self.post("1*0042")
        self.assertEqual(self.post("1*0042*7"), SAFE_RESPONSES["invalid_selection"])
        self.assertEqual(self.post("1*0042*7"), SAFE_RESPONSES["invalid_selection"])
        self.assertTrue(all(row["state"] == "ended" for row in self.db.sessions.values()))
        self.verifier.assert_called_once()

    def test_operational_failures_capacity_and_zero_row_transition_roll_back(self):
        key = self.prompt()
        self.db.credentials[91]["failed_attempts"] = 2
        before = copy.deepcopy((self.db.sessions, self.db.callbacks, self.db.credentials))
        self.verifier.side_effect = verify_ussd_pin
        with scrypt_slot():
            self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
        self.verifier.side_effect = UssdPinError("synthetic-secret")
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
        self.verifier.side_effect = self.verify
        for failure in ("UPDATE farmer_ussd_credentials", "INSERT INTO ussd_callback_results", "zero"):
            self.db.fail_at = None if failure == "zero" else failure
            self.db.force_zero = failure == "zero"
            self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["verification_failed"])
            self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
            self.assertEqual(self.db.sessions[key]["state"], "pin")

    def test_cancel_and_verified_exit_are_atomic_on_persistence_failure(self):
        key = self.prompt()
        self.db.fail_at = "UPDATE ussd_sessions SET last_input_count"
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.assertEqual(self.post("1*0"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.db.fail_at = None
        self.post("1*0042")
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.db.fail_at = "UPDATE ussd_sessions SET last_input_count"
        self.assertEqual(self.post("1*0042*0"), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.assertEqual(self.db.sessions[key]["state"], "verified")

    def test_invalid_numeric_pin_terminates_without_counting_a_wrong_pin(self):
        key = self.prompt()
        self.assertEqual(self.post("1*123"), SAFE_RESPONSES["invalid_selection"])
        self.assertEqual(self.db.sessions[key]["state"], "ended")
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 0)
        self.verifier.assert_not_called()

    def test_replay_text_is_not_an_authorization_grant(self):
        self.prompt()
        self.post("1*0042")
        with self.app.app_context():
            replay = authenticate_ussd_pin(PHONE, "menu-session", CODE, "1*0042")
        self.assertFalse(replay["authenticated"])
        self.assertEqual(replay["response"], SAFE_RESPONSES["pin_verified"])
        self.post("1*0042*1")
        with self.app.app_context():
            replay = advance_ussd_menu(PHONE, "menu-session", CODE, "1*0042*1")
        self.assertFalse(replay["authenticated"])
        self.verifier.assert_called_once()

    def test_no_secrets_or_protected_data_in_responses_logs_or_sql(self):
        with self.assertNoLogs(level="DEBUG"):
            self.prompt()
            success = self.post("1*0042")
            self.post("1*0042*0")
        for value in (PHONE, "0042", RELAY, METADATA, self.credential["pin_hash"], "farmer_id", "user_id"):
            self.assertNotIn(value, success)
        self.assertNotIn("1*0042", repr(self.db.queries))

    def test_disabled_legacy_and_ingress_limits_never_reach_authentication(self):
        with patch("app.services.ussd_authentication_service.identify_ussd_candidate") as identify:
            self.app.config["USSD_PERSISTENT_FLOW_ENABLED"] = False
            self.assertEqual(self.post("1"), SAFE_RESPONSES["verification_unavailable"])
            self.app.config["USSD_PERSISTENT_FLOW_ENABLED"] = True
            self.limiter.decision = False
            self.post("1")
            identify.assert_not_called()
        self.guard.assert_not_called()
        self.assertEqual(self.db.connections, [])

    def test_simultaneous_duplicate_pin_callbacks_hash_outside_locks_and_count_once(self):
        key = self.prompt()
        barrier = Barrier(2)
        def verify(*args):
            self.assertFalse(getattr(self.db.thread, "in_transaction", False))
            barrier.wait(timeout=5)
            return False
        self.verifier.side_effect = verify
        def attempt():
            return self.post("1*9999", client=self.app.test_client())
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(results, [SAFE_RESPONSES["verification_failed"]] * 2)
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 1)
        self.assertEqual(self.db.sessions[key]["state"], "ended")

    def test_cancellation_during_hash_wins_without_counter_reset_or_success_result(self):
        key = self.prompt()
        self.db.credentials[91]["failed_attempts"] = 2
        started, release = Event(), Event()
        def verify(*args):
            self.assertFalse(getattr(self.db.thread, "in_transaction", False))
            started.set()
            if not release.wait(timeout=5):
                raise RuntimeError("synthetic test timeout")
            return True
        self.verifier.side_effect = verify
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: self.post("1*0042", client=self.app.test_client()))
            try:
                self.assertTrue(started.wait(timeout=5))
                self.assertEqual(self.post("1*0"), SAFE_RESPONSES["exit"])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=5), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.db.credentials[91]["failed_attempts"], 2)
        self.assertEqual(self.db.sessions[key]["state"], "ended")
        self.assertEqual(self.db.callbacks[key, 2]["response_text"], SAFE_RESPONSES["exit"])
        self.assertIsNone(self.db.sessions[key]["authenticated_at"])

    def test_database_expiry_during_final_menu_update_rolls_back_result_and_state(self):
        for action in ("1*0", "1*0042*1", "1*0042*0", "0"):
            with self.subTest(action=action):
                self.db.before_menu_update = lambda: None
                self.db.current_time = lambda: NOW
                sid = "expiry-" + action.replace("*", "-")
                if action == "0":
                    self.post(sid=sid)
                else:
                    self.prompt(sid)
                    if action.startswith("1*0042"):
                        self.post("1*0042", sid)
                before = copy.deepcopy((self.db.sessions, self.db.callbacks, self.db.credentials))
                def expire():
                    # The result has been inserted; the final SQL guard must
                    # reject it even though the application clock stays stale.
                    self.assertGreater(len(self.db.callbacks), len(before[1]))
                    self.db.current_time = lambda: NOW + timedelta(seconds=180)
                self.db.before_menu_update = expire
                self.assertEqual(self.post(action, sid), SAFE_RESPONSES["verification_failed"])
                self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
                connection = self.db.connections[-1]
                connection.rollback.assert_called_once_with()
                connection.commit.assert_not_called()
                final_sql = self.db.queries[-1][0]
                for predicate in ("state = %s", "last_input_count = %s", "farmer_id <=> %s",
                                  "phone_tag = %s", "service_code = %s", "verified_pin_version <=> %s",
                                  "authenticated_at <=> %s", "expires_at > UTC_TIMESTAMP(6)"):
                    self.assertIn(predicate, final_sql)

    def test_zero_row_menu_update_rolls_back_navigation_cancellation_and_exit(self):
        for action in ("1*0", "1*0042*1", "1*0042*0", "0"):
            with self.subTest(action=action):
                self.db.force_menu_zero = False
                sid = "zero-" + action.replace("*", "-")
                if action == "0":
                    self.post(sid=sid)
                else:
                    self.prompt(sid)
                    if action.startswith("1*0042"):
                        self.post("1*0042", sid)
                before = copy.deepcopy((self.db.sessions, self.db.callbacks, self.db.credentials))
                self.db.force_menu_zero = True
                self.assertEqual(self.post(action, sid), SAFE_RESPONSES["verification_failed"])
                self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
                self.db.connections[-1].rollback.assert_called_once_with()
                self.db.connections[-1].commit.assert_not_called()

    def test_identification_rejects_missing_or_corrupted_welcome_history(self):
        for corruption in ("missing", "fingerprint", "response", "status"):
            sid = "welcome-" + corruption
            self.post(sid=sid)
            key = derive_session_key(base64.b64decode(METADATA), CODE, sid)
            self.corrupt_history(key, 0, corruption)
            before = copy.deepcopy((self.db.sessions, self.db.callbacks))
            self.assertEqual(self.post("1", sid), SAFE_RESPONSES["verification_failed"])
            self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.verifier.assert_not_called()

    def corrupt_history(self, key, position, corruption):
        if corruption == "missing":
            del self.db.callbacks[key, position]
        else:
            field, value = {"fingerprint": ("input_fingerprint", b"x" * 32),
                            "response": ("response_text", SAFE_RESPONSES["verification_failed"]),
                            "status": ("response_status", 503)}[corruption]
            self.db.callbacks[key, position][field] = value

    def test_pin_phase_one_rejects_missing_or_corrupted_complete_history(self):
        for position in (0, 1):
            for corruption in ("missing", "fingerprint", "response", "status"):
                sid = "phase-one-" + str(position) + corruption
                key = self.prompt(sid)
                self.corrupt_history(key, position, corruption)
                before = copy.deepcopy((self.db.sessions, self.db.callbacks, self.db.credentials))
                self.assertEqual(self.post("1*0042", sid), SAFE_RESPONSES["verification_failed"])
                self.assertEqual((self.db.sessions, self.db.callbacks, self.db.credentials), before)
        self.verifier.assert_not_called()

    def test_pin_phase_two_rechecks_complete_history_after_hashing(self):
        for position in (0, 1):
            for corruption in ("missing", "fingerprint", "response", "status"):
                sid = "phase-two-" + str(position) + corruption
                key = self.prompt(sid)
                before = copy.deepcopy((self.db.sessions, self.db.credentials))
                def verify(*args):
                    self.assertFalse(getattr(self.db.thread, "in_transaction", False))
                    self.corrupt_history(key, position, corruption)
                    return True
                self.verifier.side_effect = verify
                self.assertEqual(self.post("1*0042", sid), SAFE_RESPONSES["verification_failed"])
                self.assertEqual((self.db.sessions, self.db.credentials), before)
                self.assertNotIn((key, 2), self.db.callbacks)

    def test_cached_success_requires_valid_timestamp_and_response_status(self):
        key = self.prompt()
        self.post("1*0042")
        original = copy.deepcopy(self.db.sessions[key])
        for timestamp in (None, "invalid", NOW - timedelta(seconds=1), NOW + timedelta(seconds=1)):
            self.db.sessions[key] = dict(original, authenticated_at=timestamp)
            self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["verification_failed"])
        self.db.sessions[key] = original
        self.db.callbacks[key, 2]["response_status"] = 503
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["verification_failed"])
        self.db.callbacks[key, 2]["response_status"] = 200
        self.assertEqual(self.post("1*0042"), SAFE_RESPONSES["pin_verified"])
        self.verifier.assert_called_once()


if __name__ == "__main__":
    unittest.main()
