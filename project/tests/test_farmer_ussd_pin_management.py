"""Dashboard -> PIN service -> real repository, with an offline connector only."""

import copy
import re
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import dotenv
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.farmer_ussd_pin_service import save_farmer_ussd_pin
    from app.services.ussd_pin_service import hash_ussd_pin, verify_ussd_pin
    from app.services.ussd_hash_capacity import scrypt_slot

from test_ussd_callback_foundation import OfflineConnector
from test_ussd_authentication_service import app_for_tests


class PinManagementConnector(OfflineConnector):
    def __init__(self, password_hash):
        super().__init__()
        self.account = dict(user_id=41, farmer_id=91, role="farmer", status="active", password_hash=password_hash)
        self.credentials = {}
        self.in_transaction = False

    def connect(self):
        connection = super().connect()
        cursor = connection.cursor.return_value
        base_execute, base_rollback = cursor.execute.side_effect, connection.rollback.side_effect
        saved = copy.deepcopy(self.credentials)
        self.in_transaction = True
        def rollback():
            base_rollback()
            self.credentials = copy.deepcopy(saved)
        connection.rollback.side_effect = rollback
        connection.close.side_effect = lambda: setattr(self, "in_transaction", False)
        def execute(sql, params):
            if "FROM ussd_sessions" in sql or "FROM ussd_callback_results" in sql:
                return base_execute(sql, params)
            self.queries.append((sql, params))
            if self.fail_at and self.fail_at in sql:
                raise RuntimeError("synthetic-private-driver-detail")
            cursor.rowcount = 1
            if sql.startswith("SELECT"):
                if "FROM users" in sql:
                    row = self.account if params[0] == self.account["user_id"] else None
                elif "FROM farmers" in sql:
                    row = self.account if params[0] == self.account["user_id"] else None
                elif "FROM farmer_ussd_credentials" in sql:
                    row = self.credentials.get(params[0])
                else:
                    raise AssertionError("Unexpected offline SELECT")
                cursor.fetchone.return_value = copy.deepcopy(row)
            elif sql.startswith("INSERT INTO farmer_ussd_credentials"):
                farmer, created, updated = params
                self.credentials.setdefault(farmer, dict(farmer_id=farmer, pin_hash=None, pin_key_version=1,
                    pin_version=0, failed_attempts=0, locked_until=None, web_failed_attempts=0,
                    web_locked_until=None, created_at=created, updated_at=updated))
            elif sql.startswith("UPDATE farmer_ussd_credentials SET pin_hash"):
                encoded, key_version, updated, farmer = params
                row = self.credentials[farmer]
                row.update(pin_hash=encoded, pin_key_version=key_version, pin_version=row["pin_version"] + 1,
                           failed_attempts=0, locked_until=None, web_failed_attempts=0,
                           web_locked_until=None, updated_at=updated)
            elif sql.startswith("UPDATE farmer_ussd_credentials SET web_failed_attempts"):
                attempts, until, updated, farmer = params
                self.credentials[farmer].update(web_failed_attempts=attempts, web_locked_until=until, updated_at=updated)
            elif sql.startswith("UPDATE ussd_sessions SET state = 'ended'"):
                for row in self.sessions.values():
                    if row["farmer_id"] == params[0] and row["state"] != "ended":
                        row.update(state="ended", authenticated_at=None, verified_pin_version=None)
            else:
                raise AssertionError("Unexpected offline mutation")
            self.after_execute(sql)
        cursor.execute.side_effect = execute
        return connection


class FarmerUssdPinManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "synthetic-web-password"
        cls.password_hash = generate_password_hash(cls.password, method="scrypt")

    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        settings = app_for_tests().config
        self.app.config.update({key: settings[key] for key in (
            "SECRET_KEY", "USSD_METADATA_HMAC_KEY", "USSD_PIN_PEPPERS", "USSD_PIN_CURRENT_KEY_VERSION")})
        self.app.config.update(TESTING=True, USSD_PERSISTENT_FLOW_ENABLED=False)
        self.db = PinManagementConnector(self.password_hash)
        helper = patch("app.models.ussd_repository.get_db_connection", side_effect=self.db.connect)
        helper.start()
        self.addCleanup(helper.stop)
        authorization = patch("app.security.get_active_farmer_identity", return_value={
            "ok": True, "identity": {"user_id": 41, "farmer_id": 91}})
        self.authorization = authorization.start()
        self.addCleanup(authorization.stop)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=41, farmer_id=91, authenticated_at=time.time())

    def tearDown(self):
        self.guard.assert_not_called()
        for connection in self.db.connections:
            connection.close.assert_called_once_with()
            connection.cursor.return_value.close.assert_called_once_with()
        for sql, params in self.db.queries:
            self.assertEqual(sql.count("%s"), len(params))
            self.assertNotIn(self.password, repr(params))

    def token(self):
        response = self.client.get("/farmer/home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return match.group(1)

    def submit(self, **changes):
        payload = dict(csrf_token=self.token(), web_password=self.password,
                       ussd_pin="0042", pin_confirmation="0042")
        return self.client.post("/farmer/ussd-pin", data=dict(payload, **changes))

    def test_enrollment_uses_real_hash_preserves_zeroes_and_never_reflects_secrets(self):
        with self.assertNoLogs(level="DEBUG"):
            response = self.submit()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/farmer/home#ussd-pin")
        row = self.db.credentials[91]
        self.assertEqual(row["pin_version"], 1)
        with self.app.app_context():
            self.assertTrue(verify_ussd_pin(91, "0042", row["pin_hash"], row["pin_key_version"]))
        page = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Your USSD PIN was saved", page)
        for secret in ("0042", self.password, row["pin_hash"], self.app.config["USSD_PIN_PEPPERS"]):
            self.assertNotIn(secret, page)
        with self.client.session_transaction() as session:
            self.assertNotIn("0042", repr(dict(session)))
            self.assertNotIn(self.password, repr(dict(session)))
        self.assertFalse(self.app.config["USSD_PERSISTENT_FLOW_ENABLED"])

    def test_change_increments_version_and_invalidates_only_own_bound_sessions(self):
        self.assertEqual(self.submit().status_code, 303)
        previous_hash = self.db.credentials[91]["pin_hash"]
        for key, farmer, state in (("verified", 91, "verified"), ("pin", 91, "pin"),
                                   ("other", 92, "verified"), ("unbound", None, "welcome")):
            self.db.sessions[key] = dict(farmer_id=farmer, state=state,
                authenticated_at=datetime.now(timezone.utc) if state == "verified" else None,
                verified_pin_version=1 if state == "verified" else None)
        self.assertEqual(self.submit(ussd_pin="0000", pin_confirmation="0000").status_code, 303)
        row = self.db.credentials[91]
        self.assertEqual(row["pin_version"], 2)
        self.assertNotEqual(row["pin_hash"], previous_hash)
        for key in ("verified", "pin"):
            self.assertEqual(self.db.sessions[key]["state"], "ended")
            self.assertIsNone(self.db.sessions[key]["authenticated_at"])
            self.assertIsNone(self.db.sessions[key]["verified_pin_version"])
        self.assertEqual(self.db.sessions["other"]["state"], "verified")
        self.assertEqual(self.db.sessions["unbound"]["state"], "welcome")
        with self.app.app_context():
            self.assertTrue(verify_ussd_pin(91, "0000", row["pin_hash"], row["pin_key_version"]))
            self.assertFalse(verify_ussd_pin(91, "0042", row["pin_hash"], row["pin_key_version"]))

    def test_incorrect_password_does_not_set_pin_or_invalidate_sessions(self):
        response = self.submit(web_password="wrong-synthetic-password")
        self.assertEqual(response.status_code, 403)
        row = self.db.credentials[91]
        self.assertIsNone(row["pin_hash"])
        self.assertEqual(row["pin_version"], 0)
        self.assertEqual(row["web_failed_attempts"], 1)
        self.assertEqual(row["failed_attempts"], 0)
        self.assertNotIn("wrong-synthetic-password", response.get_data(as_text=True))

    def test_invalid_pins_and_mismatch_are_rejected_before_hashing_or_sql(self):
        for pin in ("123", "12345", "１２３４", " 1234", "12a4", "1234\n", ""):
            with self.subTest(pin=pin):
                self.assertEqual(self.submit(ussd_pin=pin, pin_confirmation=pin).status_code, 400)
        self.assertEqual(self.submit(pin_confirmation="9999").status_code, 400)
        self.assertEqual(self.db.connections, [])

    def test_csrf_rejection_duplicate_fields_and_client_identity_injection(self):
        payload = dict(csrf_token=self.token(), web_password=self.password, ussd_pin="0042", pin_confirmation="0042")
        self.assertEqual(self.client.post("/farmer/ussd-pin", data=dict(payload, csrf_token="bad")).status_code, 403)
        self.assertEqual(self.client.post("/farmer/ussd-pin", data=dict(payload, farmer_id="92")).status_code, 400)
        duplicate = MultiDict(payload)
        duplicate.add("ussd_pin", "9999")
        self.assertEqual(self.client.post("/farmer/ussd-pin", data=duplicate).status_code, 400)
        with self.client.session_transaction() as session:
            session["ussd_pin_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.post("/farmer/ussd-pin", data=payload).status_code, 403)
        self.assertEqual(self.db.connections, [])

    def test_unauthorized_expired_or_inactive_sessions_cannot_manage_pin(self):
        for identity in ({}, {"user_id": 41, "farmer_id": 91, "authenticated_at": time.time() - 1801}):
            with self.client.session_transaction() as session:
                session.clear()
                session.update(identity)
            response = self.client.post("/farmer/ussd-pin", data={})
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.session_transaction() as session:
            session.update(user_id=41, farmer_id=91, authenticated_at=time.time())
        self.authorization.return_value = {"ok": False, "code": "invalid_session"}
        self.assertEqual(self.client.post("/farmer/ussd-pin", data={}).status_code, 401)
        self.assertEqual(self.db.connections, [])

    def test_current_role_status_and_profile_are_rechecked_in_repository(self):
        for field, value in (("role", "officer"), ("status", "disabled"), ("farmer_id", 92)):
            original = dict(self.db.account)
            self.db.account[field] = value
            self.assertEqual(self.submit().status_code, 503)
            self.db.account = original
        self.assertEqual(self.db.credentials, {})

    def test_five_password_failures_lock_only_pin_management_and_expiry_resets_window(self):
        for index in range(5):
            self.assertEqual(self.submit(web_password="wrong").status_code, 429 if index == 4 else 403)
        before = copy.deepcopy(self.db.credentials)
        self.assertEqual(self.submit().status_code, 429)
        self.assertEqual(self.db.credentials, before)
        until = self.db.credentials[91]["web_locked_until"].replace(tzinfo=timezone.utc)
        with patch("app.services.farmer_ussd_pin_service._now", return_value=until):
            self.assertEqual(self.submit(web_password="wrong").status_code, 403)
        self.assertEqual(self.db.credentials[91]["web_failed_attempts"], 1)
        self.assertIsNone(self.db.credentials[91]["web_locked_until"])
        self.assertEqual(self.submit().status_code, 303)
        self.assertEqual(self.db.credentials[91]["web_failed_attempts"], 0)

    def test_hashes_are_outside_locks_and_changed_web_password_refuses_write(self):
        actual_hash = hash_ussd_pin
        def password_check(*args):
            self.assertFalse(self.db.in_transaction)
            return check_password_hash(*args)
        def hashing(*args):
            self.assertFalse(self.db.in_transaction)
            self.db.account["password_hash"] = "changed-password-hash"
            return actual_hash(*args)
        with patch("app.services.farmer_ussd_pin_service.hash_ussd_pin", side_effect=hashing), \
                patch("app.services.farmer_ussd_pin_service.check_password_hash", side_effect=password_check):
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(self.db.credentials, {})

    def test_failure_after_session_invalidation_rolls_back_hash_version_and_sessions(self):
        self.assertEqual(self.submit().status_code, 303)
        self.db.sessions["own"] = dict(farmer_id=91, state="verified", verified_pin_version=1,
                                       authenticated_at=datetime.now(timezone.utc))
        before = copy.deepcopy((self.db.credentials, self.db.sessions))
        def fail_after_write(sql):
            if sql.startswith("UPDATE ussd_sessions SET state = 'ended'"):
                self.assertEqual(self.db.sessions["own"]["state"], "ended")
                raise RuntimeError("synthetic-private-failure")
        self.db.after_execute = fail_after_write
        self.assertEqual(self.submit().status_code, 503)
        self.assertEqual((self.db.credentials, self.db.sessions), before)
        self.db.connections[-1].commit.assert_not_called()
        self.db.connections[-1].rollback.assert_called_once_with()

    def test_capacity_missing_pepper_and_sql_failures_do_not_commit_changes(self):
        with scrypt_slot():
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(self.db.credentials, {})
        with patch.dict(self.app.config, USSD_PIN_PEPPERS=None):
            self.assertEqual(self.submit().status_code, 503)
        self.assertEqual(self.db.credentials, {})
        for failure in ("UPDATE farmer_ussd_credentials SET pin_hash", "UPDATE ussd_sessions SET state = 'ended'"):
            self.db.fail_at = failure
            self.assertEqual(self.submit().status_code, 503)
            self.assertEqual(self.db.credentials, {})
            self.db.connections[-1].commit.assert_not_called()
            self.db.connections[-1].rollback.assert_called_once_with()

    def test_service_rejects_untrusted_identities_without_sql(self):
        with self.app.app_context():
            for user_id, farmer_id in ((True, 91), (41, None), (0, 91), (41, -1)):
                self.assertFalse(save_farmer_ussd_pin(user_id, farmer_id, self.password, "0042", "0042")["ok"])
        self.assertEqual(self.db.connections, [])

    def test_form_is_bounded_and_success_rotates_csrf_token(self):
        token = self.token()
        self.assertEqual(self.submit().status_code, 303)
        self.assertNotEqual(self.token(), token)
        self.assertEqual(self.client.post("/farmer/ussd-pin", json={}).status_code, 415)
        self.assertEqual(self.client.post("/farmer/ussd-pin", data="x" * 4097,
                         content_type="application/x-www-form-urlencoded").status_code, 413)


if __name__ == "__main__":
    unittest.main()
