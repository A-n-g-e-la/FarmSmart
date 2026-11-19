import time
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error
from werkzeug.security import generate_password_hash

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.officer_authentication_service import authenticate_officer, get_active_officer_identity


class OfficerAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = " synthetic-officer-password "
        cls.password_hash = generate_password_hash(cls.password, method="scrypt")

    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.user = {"user_id": 41, "agricultural_officer_id": 91, "role": "officer",
                     "status": "active", "password_hash": self.password_hash, "approved_at": datetime(2026, 1, 1)}
        self.cursor.fetchone.return_value = self.user
        helper = patch("app.services.officer_authentication_service.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.guard.assert_not_called()

    def assert_invalid(self, result):
        self.assertEqual(result, {"ok": False, "code": "invalid_credentials", "message": "Invalid email or password."})

    def test_login_normalizes_email_uses_parameters_and_returns_only_identity(self):
        result = authenticate_officer(" Officer@Example.COM ", self.password)
        self.assertEqual(result, {"ok": True, "identity": {"user_id": 41, "agricultural_officer_id": 91}})
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("LEFT JOIN agricultural_officers AS o ON o.user_id = u.user_id", sql)
        self.assertIn("WHERE u.email = %s", sql)
        self.assertEqual(params, ("officer@example.com",))
        self.assertNotIn("officer@example.com", sql)
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        self.assertNotIn(self.password, str(result))
        self.assertNotIn(self.password_hash, str(result))

    def test_invalid_password_and_unknown_account_are_indistinguishable(self):
        self.assert_invalid(authenticate_officer("officer@example.com", "wrong-password"))
        self.cursor.fetchone.return_value = None
        self.assert_invalid(authenticate_officer("officer@example.com", self.password))
        with patch("app.services.officer_authentication_service.check_password_hash", return_value=True) as verify:
            self.assert_invalid(authenticate_officer("officer@example.com", self.password))
        verify.assert_called_once()
        self.assertTrue(verify.call_args.args[0].startswith("scrypt:"))

    def test_pending_disabled_farmer_missing_profile_and_unapproved_are_generic(self):
        for changes in ({"status": "pending"}, {"status": "disabled"}, {"role": "farmer"},
                        {"agricultural_officer_id": None}, {"approved_at": None}):
            with self.subTest(changes=changes):
                self.cursor.fetchone.return_value = dict(self.user, **changes)
                self.assert_invalid(authenticate_officer("officer@example.com", self.password))

    def test_password_whitespace_is_preserved(self):
        self.assertTrue(authenticate_officer("officer@example.com", self.password)["ok"])
        self.assert_invalid(authenticate_officer("officer@example.com", self.password.strip()))

    def test_malformed_or_oversized_credentials_do_not_query_database(self):
        for email in (None, "invalid", "a..b@example.com", "a@localhost", "a@example.com' OR 1=1", 123):
            self.assert_invalid(authenticate_officer(email, self.password))
        for password in (None, "", [], 123, "x" * 1025):
            self.assert_invalid(authenticate_officer("officer@example.com", password))
        self.helper.assert_not_called()

    def test_authentication_database_and_verification_errors_are_safe(self):
        for stage in ("connection", "cursor", "query", "verification"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                target = {"connection": self.helper, "cursor": self.connection.cursor, "query": self.cursor.execute}.get(stage)
                if target is not None:
                    target.side_effect = Error("private SQL credentials")
                with patch("app.services.officer_authentication_service.check_password_hash", side_effect=ValueError("private hash") if stage == "verification" else None, return_value=True):
                    with self.assertLogs("app.services.officer_authentication_service", level="ERROR") as logs:
                        result = authenticate_officer("officer@example.com", self.password)
                self.assertEqual(result["code"], "authentication_error")
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                if stage != "connection":
                    self.connection.close.assert_called_once_with()
                if stage in ("query", "verification"):
                    self.cursor.close.assert_called_once_with()
                if target is not None:
                    target.side_effect = None

    def test_cleanup_failures_preserve_safe_result_and_close_connection(self):
        self.cursor.close.side_effect = Error("private")
        self.connection.close.side_effect = Error("private")
        with self.assertLogs("app.services.officer_authentication_service", level="WARNING") as logs:
            result = authenticate_officer("officer@example.com", self.password)
        self.assertTrue(result["ok"])
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private", str(logs.output))

    def test_revalidation_scopes_user_profile_role_status_and_approval_without_password(self):
        result = get_active_officer_identity(41, 91)
        self.assertEqual(result, {"ok": True, "identity": {"user_id": 41, "agricultural_officer_id": 91}})
        sql, params = self.cursor.execute.call_args.args
        self.assertIn("o.user_id = u.user_id", sql)
        self.assertIn("u.user_id = %s AND o.agricultural_officer_id = %s", sql)
        self.assertIn("u.role = 'officer' AND u.status = 'active'", sql)
        self.assertIn("o.approved_at IS NOT NULL", sql)
        self.assertNotIn("password", sql)
        self.assertNotIn("farmers", sql)
        self.assertEqual(params, (41, 91))
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_missing_revalidation_row_revokes_access(self):
        self.cursor.fetchone.return_value = None
        self.assertEqual(get_active_officer_identity(41, 91), {"ok": False, "code": "invalid_session"})

    def test_revalidation_outages_are_safe_and_resources_are_released(self):
        self.cursor.execute.side_effect = Error("private")
        with self.assertLogs("app.services.officer_authentication_service", level="ERROR") as logs:
            self.assertEqual(get_active_officer_identity(41, 91), {"ok": False, "code": "authentication_error"})
        self.assertNotIn("private", str(logs.output))
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()


class OfficerSessionTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-officer-auth-key")
        self.client = self.app.test_client()
        self.identity = {"user_id": 41, "agricultural_officer_id": 91}
        auth = patch("app.routes.officer.authenticate_officer", return_value={"ok": True, "identity": self.identity})
        self.auth = auth.start()
        self.addCleanup(auth.stop)
        lookup = patch("app.security.get_active_officer_identity", return_value={"ok": True, "identity": self.identity})
        self.lookup = lookup.start()
        self.addCleanup(lookup.stop)

    def tearDown(self):
        self.guard.assert_not_called()

    def login(self, form=False, **extra):
        token = self.client.get("/officer/login").json["csrf_token"]
        data = {"email": " Officer@Example.COM ", "password": "synthetic-password", **extra}
        return self.client.post("/officer/login", headers={"X-CSRF-Token": token}, **({"data": data} if form else {"json": data}))

    def test_login_replaces_session_and_ignores_client_identity_or_privileges(self):
        with self.client.session_transaction() as session:
            session.update(user_id=999, farmer_id=999, arbitrary="old", logout_csrf="old-token")
        response = self.login(user_id=666, agricultural_officer_id=666, role="farmer", status="active", approved_at="now")
        self.assertEqual(response.status_code, 200)
        self.auth.assert_called_once_with(" Officer@Example.COM ", "synthetic-password")
        self.assertEqual(response.json["identity"], self.identity)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.session_transaction() as session:
            self.assertEqual(set(session), {"user_id", "agricultural_officer_id", "authenticated_at", "_permanent", "officer_logout_csrf", "officer_logout_csrf_issued"})
            self.assertEqual(session["user_id"], 41)
            self.assertEqual(session["agricultural_officer_id"], 91)
            self.assertTrue(session.permanent)
            self.assertNotIn("synthetic-password", str(dict(session)))
        cookie = response.headers["Set-Cookie"]
        for attribute in ("HttpOnly", "SameSite=Lax", "Expires="):
            self.assertIn(attribute, cookie)

    def test_form_login_and_body_token_follow_farmer_conventions(self):
        self.assertEqual(self.login(form=True).status_code, 200)
        token = self.client.get("/officer/login").json["csrf_token"]
        self.assertEqual(self.client.post("/officer/login", json={"email": "officer@example.com", "password": "synthetic-password", "csrf_token": token}).status_code, 200)

    def test_login_csrf_rejects_missing_invalid_unicode_other_scopes_and_other_sessions(self):
        token = self.client.get("/officer/login").json["csrf_token"]
        farmer_token = self.client.get("/farmer/login").json["csrf_token"]
        registration_token = self.client.get("/officer/register").json["csrf_token"]
        for invalid in ("", "x" * len(token), "é" * len(token), farmer_token, registration_token):
            response = self.client.post("/officer/login", json={"csrf_token": invalid})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["code"], "csrf_error")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        other = self.app.test_client()
        self.assertEqual(other.post("/officer/login", json={"csrf_token": token}).status_code, 400)
        self.auth.assert_not_called()

    def test_login_csrf_expiry_and_invalid_csrf_preserve_existing_identity(self):
        self.login()
        token = self.client.get("/officer/login").json["csrf_token"]
        with self.client.session_transaction() as session:
            session["officer_login_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.post("/officer/login", json={"csrf_token": token}).status_code, 400)
        with self.client.session_transaction() as session:
            self.assertEqual(session["agricultural_officer_id"], 91)
        self.assertNotEqual(self.client.get("/officer/login").json["csrf_token"], token)

    def test_malformed_requests_do_not_attempt_authentication(self):
        for options, expected in (({"data": "text", "content_type": "text/plain"}, 415),
                                  ({"json": []}, 400), ({"json": "text"}, 400),
                                  ({"data": "{broken", "content_type": "application/json"}, 400)):
            response = self.client.post("/officer/login", **options)
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.auth.assert_not_called()

    def test_failed_login_and_outage_clear_old_identity_and_provide_retry_token(self):
        for code, status in (("invalid_credentials", 401), ("authentication_error", 503)):
            with self.subTest(code=code):
                with self.client.session_transaction() as session:
                    session.update(user_id=999, farmer_id=999, arbitrary="old")
                self.auth.return_value = {"ok": False, "code": code, "message": "Safe error."}
                response = self.login()
                self.assertEqual(response.status_code, status)
                self.assertIn("csrf_token", response.json)
                with self.client.session_transaction() as session:
                    for name in ("user_id", "farmer_id", "agricultural_officer_id", "arbitrary"):
                        self.assertNotIn(name, session)
                self.assertEqual(self.client.get("/officer/home").status_code, 401)

    def test_protected_home_is_minimal_revalidated_json_with_logout_token(self):
        token = self.login().json["csrf_token"]
        response = self.client.get("/officer/home?user_id=666&agricultural_officer_id=666")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "identity": self.identity, "csrf_token": token})
        self.lookup.assert_called_once_with(41, 91)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_expired_future_and_malformed_sessions_do_not_revalidate(self):
        baseline = dict(self.identity, authenticated_at=time.time())
        for changes in ({"authenticated_at": time.time() - 1801}, {"authenticated_at": time.time() + 60},
                        {"user_id": True}, {"user_id": "41"}, {"agricultural_officer_id": 0},
                        {"agricultural_officer_id": None}, {"authenticated_at": "now"},
                        {"authenticated_at": float("nan")}, {"farmer_id": 123}):
            with self.subTest(changes=changes):
                with self.client.session_transaction() as session:
                    session.clear()
                    session.update(baseline, **changes)
                response = self.client.get("/officer/home")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                with self.client.session_transaction() as session:
                    self.assertFalse(dict(session))
        self.assertEqual(self.client.get("/officer/home").status_code, 401)
        self.lookup.assert_not_called()

    def test_successful_requests_do_not_extend_absolute_authentication_time(self):
        self.login()
        issued = time.time() - 100
        with self.client.session_transaction() as session:
            session["authenticated_at"] = issued
        self.assertEqual(self.client.get("/officer/home").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["authenticated_at"], issued)

    def test_revoked_identity_clears_session_and_denies_home_and_logout(self):
        for endpoint, method in (("/officer/home", "get"), ("/officer/logout", "post")):
            self.login()
            self.lookup.return_value = {"ok": False, "code": "invalid_session"}
            response = getattr(self.client, method)(endpoint)
            self.assertEqual(response.status_code, 401)
            with self.client.session_transaction() as session:
                self.assertFalse(dict(session))

    def test_revalidation_outage_denies_access_but_preserves_session_for_recovery(self):
        self.login()
        self.lookup.return_value = {"ok": False, "code": "authentication_error"}
        response = self.client.get("/officer/home")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.session_transaction() as session:
            self.assertEqual(session["agricultural_officer_id"], 91)

    def test_logout_requires_scoped_unexpired_token_and_revalidated_session(self):
        token = self.login().json["csrf_token"]
        farmer_token = self.client.get("/farmer/login").json["csrf_token"]
        login_token = self.client.get("/officer/login").json["csrf_token"]
        for invalid in ("", "wrong", farmer_token, login_token):
            self.assertEqual(self.client.post("/officer/logout", headers={"X-CSRF-Token": invalid}).status_code, 400)
        with self.client.session_transaction() as session:
            session["officer_logout_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.post("/officer/logout", headers={"X-CSRF-Token": token}).status_code, 400)
        refreshed = self.client.get("/officer/home").json["csrf_token"]
        self.assertNotEqual(refreshed, token)
        response = self.client.post("/officer/logout", headers={"X-CSRF-Token": refreshed})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.session_transaction() as session:
            self.assertFalse(dict(session))
        self.assertEqual(self.client.get("/officer/home").status_code, 401)
        self.assertEqual(self.client.post("/officer/logout").status_code, 401)

    def test_logout_form_token_and_non_mutating_get(self):
        token = self.login().json["csrf_token"]
        self.assertEqual(self.client.get("/officer/logout").status_code, 405)
        self.assertEqual(self.client.get("/officer/home").status_code, 200)
        self.assertEqual(self.client.post("/officer/logout", data={"csrf_token": token}).status_code, 200)

    def test_farmer_and_officer_sessions_cannot_access_other_roles_protected_routes(self):
        for endpoint, method in (("/officer/home", "get"), ("/officer/logout", "post")):
            with self.client.session_transaction() as session:
                session.update(user_id=51, farmer_id=101, authenticated_at=time.time())
            self.assertEqual(getattr(self.client, method)(endpoint).status_code, 401)
        self.lookup.assert_not_called()
        with patch("app.security.get_active_farmer_identity") as farmer_lookup:
            for endpoint, method in (("/farmer/home", "get"), ("/farmer/plots", "get"),
                                     ("/farmer/plots", "post"), ("/farmer/plots/1", "patch"), ("/farmer/logout", "post")):
                self.login()
                self.assertEqual(getattr(self.client, method)(endpoint).status_code, 401)
            farmer_lookup.assert_not_called()

    def test_account_switching_both_directions_replaces_identity_and_tokens(self):
        first_token = self.login().json["csrf_token"]
        farmer_identity = {"user_id": 51, "farmer_id": 101}
        with patch("app.routes.farmer.authenticate_farmer", return_value={"ok": True, "identity": farmer_identity}):
            csrf = self.client.get("/farmer/login").json["csrf_token"]
            response = self.client.post("/farmer/login", json={"phone_number": "0712345678", "password": "synthetic-password", "csrf_token": csrf})
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["farmer_id"], 101)
            self.assertNotIn("agricultural_officer_id", session)
            self.assertNotIn("officer_logout_csrf", session)
        self.identity = {"user_id": 61, "agricultural_officer_id": 111}
        self.auth.return_value = {"ok": True, "identity": self.identity}
        response = self.login()
        self.assertNotEqual(response.json["csrf_token"], first_token)
        with self.client.session_transaction() as session:
            self.assertEqual(session["agricultural_officer_id"], 111)
            self.assertNotIn("farmer_id", session)
            self.assertNotIn("logout_csrf", session)
        self.assertEqual(self.client.post("/officer/logout", headers={"X-CSRF-Token": first_token}).status_code, 400)


class OfficerAuthenticationIntegrationTests(unittest.TestCase):
    """Exercise routes, real authentication services and guard together with mock SQL."""

    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("synthetic-password", method="scrypt")

    def test_login_eligibility_and_post_login_revocation_use_actual_service(self):
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            app = create_app()
            app.config.update(TESTING=True, SECRET_KEY="isolated-officer-integration-key")
            for changes in ({}, {"role": "farmer"}, {"status": "pending"}, {"status": "disabled"},
                            {"agricultural_officer_id": None}, {"approved_at": None}):
                with self.subTest(changes=changes):
                    client = app.test_client()
                    connection = MagicMock()
                    cursor = connection.cursor.return_value
                    user = dict(user_id=41, agricultural_officer_id=91, role="officer", status="active",
                                approved_at=datetime(2026, 1, 1), password_hash=self.password_hash)
                    cursor.fetchone.return_value = dict(user, **changes)
                    with patch("app.services.officer_authentication_service.get_db_connection", return_value=connection):
                        token = client.get("/officer/login").json["csrf_token"]
                        response = client.post("/officer/login", json={"email": " Officer@Example.COM ", "password": "synthetic-password"}, headers={"X-CSRF-Token": token})
                        self.assertEqual(response.status_code, 401 if changes else 200)
                        if not changes:
                            self.assertEqual(client.get("/officer/home").status_code, 200)
                            cursor.fetchone.return_value = None  # SQL no longer matches active approved officer.
                            self.assertEqual(client.get("/officer/home").status_code, 401)
                            with client.session_transaction() as session:
                                self.assertFalse(dict(session))
                    connection.commit.assert_not_called()
            guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
