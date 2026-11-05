import base64
import copy
import json
import os
import runpy
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import IntegrityError

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.models.ussd_repository import SAFE_RESPONSES
    from app.services.ussd_callback_security import CallbackRefused, SharedCallbackLimiter, load_callback_settings


NOW = datetime(2026, 11, 5, 9, tzinfo=timezone.utc)
PHONE = "+254712345678"
CODE = "*384*123#"
RELAY = base64.b64encode(b"r" * 32).decode("ascii")  # Synthetic fixtures only.
METADATA = base64.b64encode(b"m" * 32).decode("ascii")


class OfflineSharedLimiter(SharedCallbackLimiter):
    """Recording fake, never a deployable shared limiter."""

    deployment_wide = True

    def __init__(self):
        self.calls = []
        self.decision = True
        self.error = None

    def admit(self, limits):
        self.calls.append(limits)
        if self.error:
            raise self.error
        return self.decision


class OfflineConnector:
    """Real repository boundary with rollback snapshots, not a SQL engine."""

    def __init__(self):
        self.sessions, self.callbacks = {}, {}
        self.connections, self.queries = [], []
        self.fail_at = None
        self.commit_error = False
        self.on_rollback = lambda: None
        self.hide_initial_lookup_once = False
        self.after_execute = lambda sql: None

    def connect(self):
        connection, cursor = MagicMock(), MagicMock()
        connection.cursor.return_value = cursor
        self.connections.append(connection)
        saved = copy.deepcopy((self.sessions, self.callbacks))
        def rollback():
            self.sessions, self.callbacks = copy.deepcopy(saved)
            self.on_rollback()
        connection.rollback.side_effect = rollback
        def commit():
            if self.commit_error:
                raise RuntimeError("synthetic-sensitive-driver-detail")
        connection.commit.side_effect = commit
        def execute(sql, params):
            self.queries.append((sql, params))
            if self.fail_at and self.fail_at in sql:
                raise RuntimeError("synthetic-sensitive-driver-detail")
            cursor.rowcount = 1
            if sql.startswith("SELECT"):
                if "FROM ussd_sessions" in sql:
                    row = self.sessions.get(params[0])
                    if self.hide_initial_lookup_once:
                        self.hide_initial_lookup_once = False
                        row = None
                elif "FROM ussd_callback_results" in sql:
                    row = self.callbacks.get(params)
                else:
                    raise AssertionError("Foundation must not look up accounts/credentials")
                cursor.fetchone.return_value = copy.deepcopy(row)
            elif sql.startswith("INSERT INTO ussd_sessions"):
                key, tag, code, farmer, created, expiry, retention = params
                if key in self.sessions:
                    raise IntegrityError("synthetic duplicate")
                self.sessions[key] = dict(session_key=key, phone_tag=tag, service_code=code, farmer_id=farmer,
                                          state="welcome", last_input_count=-1, created_at=created,
                                          expires_at=expiry, retain_until=retention,
                                          verified_pin_version=None, authenticated_at=None)
            elif sql.startswith("INSERT INTO ussd_callback_results"):
                key, position, digest, text, status, stamp = params
                if (key, position) in self.callbacks:
                    raise IntegrityError("synthetic duplicate")
                self.callbacks[key, position] = dict(input_fingerprint=digest, response_text=text,
                                                     response_status=status, created_at=stamp)
            elif sql.startswith("UPDATE ussd_sessions SET last_input_count"):
                self.sessions[params[1]]["last_input_count"] = params[0]
            elif sql.startswith("UPDATE ussd_sessions SET state = %s"):
                self.sessions[params[1]].update(state=params[0], verified_pin_version=None, authenticated_at=None)
            else:
                raise AssertionError("Unexpected mocked SQL")
            self.after_execute(sql)
        cursor.execute.side_effect = execute
        return connection


class UssdCallbackFoundationTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(
            TESTING=True, DEBUG=False, SECRET_KEY="synthetic-cookie-secret",
            USSD_PERSISTENT_FLOW_ENABLED=True, USSD_RELAY_SECRET=RELAY,
            USSD_TRUSTED_RELAY_IPS="10.0.0.8", USSD_METADATA_HMAC_KEY=METADATA,
            USSD_SERVICE_CODE=CODE, AFRICASTALKING_API_KEY=None,
            USSD_PIN_PEPPERS=json.dumps({"1": base64.b64encode(b"p" * 32).decode("ascii")}),
            USSD_PIN_CURRENT_KEY_VERSION="1", USSD_PHONE_CALLBACKS_PER_MINUTE=20,
            USSD_PHONE_CALLBACK_BURST=5, USSD_NEW_SESSIONS_PER_MINUTE=3,
            USSD_GLOBAL_CALLBACKS_PER_SECOND=10, USSD_GLOBAL_CALLBACK_BURST=10,
            USSD_SESSION_SECONDS=180, USSD_RETENTION_SECONDS=86400,
        )
        self.limiter = OfflineSharedLimiter()
        self.app.extensions["ussd_shared_limiter"] = self.limiter
        self.db = OfflineConnector()
        db_patch = patch("app.models.ussd_repository.get_db_connection", side_effect=self.db.connect)
        self.connection_helper = db_patch.start()
        self.addCleanup(db_patch.stop)
        self.clock = NOW
        clock = patch("app.services.ussd_callback_service._now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.forbidden = []
        for name in ("identify_ussd_candidate", "authenticate_ussd_pin"):
            forbidden = patch("app.services.ussd_authentication_service." + name,
                              side_effect=AssertionError("Authentication must remain disconnected"))
            self.forbidden.append(forbidden.start())
            self.addCleanup(forbidden.stop)
        for name in ("generate_password_hash", "check_password_hash"):
            forbidden = patch("app.services.ussd_pin_service." + name,
                              side_effect=AssertionError("No hashing in this stage"))
            self.forbidden.append(forbidden.start())
            self.addCleanup(forbidden.stop)
        self.client = self.app.test_client()
        self.payload = dict(sessionId="foundation-session", serviceCode=CODE, phoneNumber=PHONE, text="")

    def tearDown(self):
        self.guard.assert_not_called()
        for forbidden in self.forbidden:
            forbidden.assert_not_called()
        for connection in self.db.connections:
            connection.close.assert_called_once_with()
            connection.cursor.return_value.close.assert_called_once_with()
        for sql, params in self.db.queries:
            self.assertEqual(sql.count("%s"), len(params))

    def post(self, *, headers=None, peer="10.0.0.8", secure=True, **changes):
        return self.client.post("/ussd", base_url="https://localhost" if secure else "http://localhost",
                                data=dict(self.payload, **changes),
                                headers={"X-FarmAdvis-USSD-Relay": RELAY} if headers is None else headers,
                                environ_overrides={"REMOTE_ADDR": peer})

    def body(self, response):
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("Set-Cookie", response.headers)
        return response.get_data(as_text=True)

    def start(self):
        self.assertEqual(self.body(self.post()), SAFE_RESPONSES["welcome"])
        return next(iter(self.db.sessions))

    def test_feature_default_and_explicit_environment_values(self):
        path = Path(__file__).resolve().parents[1] / "app" / "config" / "__init__.py"
        for value, expected in ((None, False), ("false", False), ("true", True), ("yes", "invalid")):
            env = {} if value is None else {"USSD_PERSISTENT_FLOW_ENABLED": value}
            with patch.dict(os.environ, env, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
                config = runpy.run_path(str(path))["Config"]
                self.assertEqual(config.USSD_PERSISTENT_FLOW_ENABLED, expected)

    def test_disabled_preserves_all_existing_menu_paths_without_infrastructure(self):
        self.app.config.update(USSD_PERSISTENT_FLOW_ENABLED=False, USSD_RELAY_SECRET=None)
        self.app.extensions.clear()
        for text, code in (("", "welcome"), ("0", "exit"), ("1", "verification_unavailable"),
                           ("2", "invalid_selection"), ("1*0042", "invalid_selection")):
            self.assertEqual(self.body(self.post(text=text, headers={}, secure=False)), SAFE_RESPONSES[code])
        self.connection_helper.assert_not_called()

    def test_missing_malformed_or_reused_security_configuration_fails_before_database(self):
        changes = (
            {"USSD_RELAY_SECRET": None}, {"USSD_RELAY_SECRET": "malformed"},
            {"USSD_RELAY_SECRET": METADATA}, {"USSD_METADATA_HMAC_KEY": "bad"},
            {"SECRET_KEY": None}, {"SECRET_KEY": RELAY}, {"SECRET_KEY": base64.b64decode(RELAY)},
            {"AFRICASTALKING_API_KEY": RELAY}, {"AT_API_KEY": RELAY},
            {"USSD_PIN_PEPPERS": json.dumps({"1": RELAY})},
            {"USSD_PIN_PEPPERS": "{}"}, {"USSD_PIN_CURRENT_KEY_VERSION": "2"},
            {"USSD_TRUSTED_RELAY_IPS": None}, {"USSD_TRUSTED_RELAY_IPS": "0.0.0.0/0"},
            {"USSD_TRUSTED_RELAY_IPS": "0.0.0.0"}, {"USSD_TRUSTED_RELAY_IPS": "10.0.0.8,10.0.0.8"},
            {"USSD_SERVICE_CODE": None}, {"USSD_PERSISTENT_FLOW_ENABLED": "true"},
            {"USSD_PHONE_CALLBACKS_PER_MINUTE": 0}, {"USSD_PHONE_CALLBACK_BURST": 21},
            {"USSD_GLOBAL_CALLBACKS_PER_SECOND": True}, {"USSD_NEW_SESSIONS_PER_MINUTE": -1},
            {"USSD_SESSION_SECONDS": 181}, {"USSD_RETENTION_SECONDS": 1},
        )
        original = dict(self.app.config)
        for change in changes:
            with self.subTest(fields=list(change)):
                self.app.config.update(change)
                response = self.post()
                self.assertEqual(response.status_code, 503)
                self.assertTrue(self.body(response).startswith("END "))
                self.app.config.clear()
                self.app.config.update(original)
        self.connection_helper.assert_not_called()
        self.assertEqual(self.limiter.calls, [])

    def test_debug_mode_is_rejected_without_parsing_pin_input(self):
        self.app.config["DEBUG"] = True
        self.assertEqual(self.post(text="1*0042").status_code, 503)
        self.connection_helper.assert_not_called()
        self.assertEqual(self.limiter.calls, [])

    def test_unauthorized_peer_secret_transport_and_forwarded_headers_are_refused(self):
        for options in ({"headers": {}}, {"headers": {"X-FarmAdvis-USSD-Relay": "x" * 44}},
                        {"peer": "198.51.100.9"}, {"secure": False},
                        {"peer": "198.51.100.9", "headers": {"X-FarmAdvis-USSD-Relay": RELAY,
                         "X-Forwarded-For": "10.0.0.8", "Origin": "https://provider.example"}}):
            self.assertEqual(self.post(**options).status_code, 403)
        self.connection_helper.assert_not_called()
        self.assertEqual(self.limiter.calls, [])

    def test_proxyfix_identity_rewriting_is_not_accepted(self):
        response = self.client.post("/ussd", base_url="https://localhost", data=self.payload,
                                    headers={"X-FarmAdvis-USSD-Relay": RELAY}, environ_overrides={
                                        "REMOTE_ADDR": "10.0.0.8", "werkzeug.proxy_fix.orig": {}})
        self.assertEqual(response.status_code, 403)
        self.connection_helper.assert_not_called()

    def test_missing_or_local_limiter_is_unavailable(self):
        for adapter in (None, object(), OfflineSharedLimiter()):
            if isinstance(adapter, OfflineSharedLimiter):
                adapter.deployment_wide = False
            self.app.extensions["ussd_shared_limiter"] = adapter
            self.assertEqual(self.post().status_code, 503)
        self.connection_helper.assert_not_called()

    def test_limiter_failure_bad_result_and_capacity_denial_precede_database(self):
        for decision, error, status in ((False, None, 429), (1, None, 503), (True, RuntimeError("secret"), 503)):
            self.limiter.decision, self.limiter.error = decision, error
            self.assertEqual(self.post().status_code, status)
        self.connection_helper.assert_not_called()

    def test_phone_limiter_denial_is_fail_closed_and_has_no_raw_identifier(self):
        def deny_phone(limits):
            self.limiter.calls.append(limits)
            return limits[0].scope == "global"
        self.limiter.admit = deny_phone
        self.assertEqual(self.post().status_code, 429)
        self.connection_helper.assert_not_called()
        phone, initial = self.limiter.calls[1]
        self.assertEqual((phone.count, phone.seconds, phone.burst), (20, 60, 5))
        self.assertEqual((initial.count, initial.seconds, initial.burst), (3, 60, 3))
        self.assertEqual(len(phone.key), 32)
        self.assertEqual(phone.key, initial.key)
        self.assertNotIn(PHONE, repr(self.limiter.calls))

    def test_welcome_is_atomic_and_only_keyed_metadata_is_stored(self):
        key = self.start()
        row = self.db.sessions[key]
        self.assertEqual(row["state"], "welcome")
        self.assertIsNone(row["farmer_id"])
        self.assertEqual(row["last_input_count"], 0)
        self.assertEqual(row["expires_at"], (NOW + timedelta(seconds=180)).replace(tzinfo=None))
        self.assertEqual(row["retain_until"], (NOW + timedelta(days=1)).replace(tzinfo=None))
        self.assertEqual(self.db.callbacks[key, 0]["response_text"], SAFE_RESPONSES["welcome"])
        self.assertNotIn(PHONE, repr(self.db.queries))
        self.assertNotIn(self.payload["sessionId"], repr(self.db.queries))
        self.db.connections[0].commit.assert_called_once_with()
        self.assertEqual((self.limiter.calls[0][0].count, self.limiter.calls[0][0].seconds,
                          self.limiter.calls[0][0].burst), (10, 1, 10))

    def test_exact_initial_duplicate_does_not_insert_or_extend_lifetime(self):
        self.start()
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.clock += timedelta(seconds=10)
        self.db.queries.clear()
        self.assertEqual(self.body(self.post(phoneNumber="0712 345 678")), SAFE_RESPONSES["welcome"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in self.db.queries))

    def test_conflicting_session_phone_or_history_never_overwrites(self):
        key = self.start()
        before = copy.deepcopy(self.db.sessions)
        self.assertEqual(self.body(self.post(phoneNumber="+254733333333")), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.db.sessions, before)
        self.db.callbacks[key, 0]["input_fingerprint"] = b"x" * 32
        self.assertEqual(self.body(self.post()), SAFE_RESPONSES["verification_failed"])

    def test_wrong_service_code_is_rejected_before_database(self):
        self.assertEqual(self.post(serviceCode="*123#").status_code, 400)
        self.connection_helper.assert_not_called()

    def test_missing_history_and_out_of_order_pin_inputs_are_refused(self):
        for text in ("0", "1", "1*0042"):
            self.assertEqual(self.body(self.post(text=text)), SAFE_RESPONSES["verification_failed"])
        key = self.start()
        self.assertEqual(self.body(self.post(text="1*0042")), SAFE_RESPONSES["verification_failed"])
        del self.db.callbacks[key, 0]
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.db.sessions[key]["state"], "welcome")

    def test_terminal_ordering_and_exact_duplicate_without_further_effects(self):
        key = self.start()
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["exit"])
        mutations = [sql for sql, _ in self.db.queries if not sql.startswith("SELECT")]
        self.assertTrue(mutations[-3].startswith("INSERT INTO ussd_callback_results"))
        self.assertTrue(mutations[-1].startswith("UPDATE ussd_sessions SET state"))
        self.assertEqual(self.db.sessions[key]["state"], "ended")
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.db.queries.clear()
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["exit"])
        self.assertEqual(self.body(self.post(text="1")), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.body(self.post()), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in self.db.queries))

    def test_advice_and_invalid_selection_terminate_without_identification(self):
        for text, code in (("1", "verification_unavailable"), ("2", "invalid_selection")):
            self.post(sessionId="choice" + text)
            self.assertEqual(self.body(self.post(sessionId="choice" + text, text=text)), SAFE_RESPONSES[code])
        self.assertTrue(all(row["state"] == "ended" for row in self.db.sessions.values()))

    def test_expired_initial_or_new_terminal_callback_cannot_reopen(self):
        key = self.start()
        self.clock += timedelta(seconds=180)
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        for text in ("", "0"):
            self.assertEqual(self.body(self.post(text=text)), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.assertEqual(self.db.sessions[key]["state"], "welcome")

    def test_expired_exact_terminal_replay_is_refused_without_writes(self):
        self.start()
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["exit"])
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.clock += timedelta(seconds=180)
        self.db.queries.clear()
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.assertTrue(all(sql.startswith("SELECT") for sql, _ in self.db.queries))

    def test_browser_identity_is_not_used_or_changed_by_enabled_foundation(self):
        identity = {"user_id": 41, "farmer_id": 91, "authenticated_at": 1}
        with self.client.session_transaction() as session:
            session.update(identity)
        key = self.start()
        self.assertIsNone(self.db.sessions[key]["farmer_id"])
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), identity)

    def test_bound_pin_or_verified_sessions_are_not_touched_by_foundation(self):
        key = self.start()
        for changes in ({"farmer_id": 91}, {"state": "pin"}, {"state": "verified"},
                        {"verified_pin_version": 1}, {"authenticated_at": NOW}):
            original = dict(self.db.sessions[key])
            self.db.sessions[key].update(changes)
            for text in ("", "0"):
                self.assertEqual(self.body(self.post(text=text)), SAFE_RESPONSES["verification_failed"])
            self.db.sessions[key] = original

    def test_creation_or_callback_failure_rolls_back_partial_welcome(self):
        for fragment in ("INSERT INTO ussd_sessions", "INSERT INTO ussd_callback_results"):
            self.db.fail_at = fragment
            self.assertEqual(self.body(self.post()), SAFE_RESPONSES["verification_failed"])
            self.assertEqual(self.db.sessions, {})
            self.assertEqual(self.db.callbacks, {})
        self.assertTrue(all(not connection.commit.called for connection in self.db.connections))

    def test_terminal_failure_rolls_back_callback_and_position(self):
        self.start()
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.db.fail_at = "UPDATE ussd_sessions SET state"
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)
        self.db.connections[-1].rollback.assert_called_once_with()
        self.db.connections[-1].commit.assert_not_called()

    def test_initial_failure_reconciles_only_an_exact_committed_winner_without_retrying_insert(self):
        self.start()
        winner = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.db.sessions, self.db.callbacks = {}, {}
        self.db.commit_error = True
        def winner_after_rollback():
            self.db.sessions, self.db.callbacks = copy.deepcopy(winner)
            self.db.commit_error = False
        self.db.on_rollback = winner_after_rollback
        self.db.queries.clear()
        self.assertEqual(self.body(self.post()), SAFE_RESPONSES["welcome"])
        self.assertEqual(sum(sql.startswith("INSERT INTO ussd_sessions") for sql, _ in self.db.queries), 1)
        self.assertEqual((self.db.sessions, self.db.callbacks), winner)

    def test_duplicate_insert_collision_reconciles_in_a_fresh_read_only_transaction(self):
        self.start()
        winner = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.db.hide_initial_lookup_once = True
        self.db.queries.clear()
        self.assertEqual(self.body(self.post()), SAFE_RESPONSES["welcome"])
        self.assertEqual((self.db.sessions, self.db.callbacks), winner)
        self.assertEqual(sum(sql.startswith("INSERT INTO ussd_sessions") for sql, _ in self.db.queries), 1)
        self.db.connections[-2].rollback.assert_called_once_with()
        self.db.connections[-2].commit.assert_not_called()

    def test_delays_during_welcome_or_terminal_writes_roll_back_expired_work(self):
        def delay(sql):
            if sql.startswith("UPDATE ussd_sessions SET last_input_count"):
                self.clock = NOW + timedelta(seconds=180)
        self.db.after_execute = delay
        self.assertEqual(self.body(self.post()), SAFE_RESPONSES["verification_failed"])
        self.assertEqual(self.db.sessions, {})
        self.assertEqual(self.db.callbacks, {})
        self.clock = NOW
        self.db.after_execute = lambda sql: None
        self.start()
        before = copy.deepcopy((self.db.sessions, self.db.callbacks))
        self.db.after_execute = delay
        self.assertEqual(self.body(self.post(text="0")), SAFE_RESPONSES["verification_failed"])
        self.assertEqual((self.db.sessions, self.db.callbacks), before)

    def test_safe_errors_and_no_pin_or_secret_logging(self):
        with self.assertNoLogs(level="DEBUG"):
            self.post(text="1*0042")
            self.db.fail_at = "INSERT"
            response = self.post()
        output = self.body(response)
        for sensitive in ("0042", PHONE, RELAY, METADATA, "synthetic-sensitive-driver-detail"):
            self.assertNotIn(sensitive, output)
        self.assertNotIn("0042", repr(self.db.queries))

    def test_unexpected_orchestrator_error_is_contained_even_in_testing_mode(self):
        with patch("app.routes.ussd.orchestrate_callback", side_effect=ValueError("secret 0042")):
            response = self.post(text="1*0042")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("0042", self.body(response))

    def test_enabled_parser_still_rejects_oversized_and_non_form_requests(self):
        self.assertEqual(self.post(text="1" * 4096).status_code, 413)
        response = self.client.post("/ussd", base_url="https://localhost", json=self.payload,
                                    headers={"X-FarmAdvis-USSD-Relay": RELAY},
                                    environ_overrides={"REMOTE_ADDR": "10.0.0.8"})
        self.assertEqual(response.status_code, 415)
        self.connection_helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
