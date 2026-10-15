import time
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

# Never read local credentials or allow a real connector call during these tests.
with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.authentication_service import get_active_farmer_identity


class DashboardParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = {}
        self.urls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements[attrs.get("id")] = (tag, attrs)
        self.urls.extend(attrs[key] for key in ("href", "src", "action") if key in attrs)


class FarmerSessionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-session-test-key")
        self.client = self.app.test_client()
        self.identity = {"user_id": 41, "farmer_id": 91}
        auth = patch("app.routes.farmer.authenticate_farmer", return_value={"ok": True, "identity": self.identity})
        self.auth = auth.start()
        self.addCleanup(auth.stop)
        lookup = patch("app.security.get_active_farmer_identity", return_value={"ok": True, "identity": self.identity})
        self.lookup = lookup.start()
        self.addCleanup(lookup.stop)
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)

    def login(self, form=False):
        token = self.client.get("/farmer/login").json["csrf_token"]
        data = {"phone_number": "0712345678", "password": "synthetic-password", "csrf_token": token}
        return self.client.post("/farmer/login", **({"data": data} if form else {"json": data}))

    def test_login_clears_old_session_and_keeps_minimal_identity(self):
        with self.client.session_transaction() as session:
            session.update(user_id=999, farmer_id=999, arbitrary="old-state", registration_csrf="old-token")
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.auth.assert_called_once_with("0712345678", "synthetic-password")
        with self.client.session_transaction() as session:
            self.assertEqual(set(session), {"user_id", "farmer_id", "authenticated_at", "_permanent", "logout_csrf", "logout_csrf_issued"})
            self.assertEqual(session["user_id"], 41)
            self.assertEqual(session["farmer_id"], 91)
            self.assertTrue(session.permanent)
            self.assertNotIn("synthetic-password", str(dict(session)))
        self.assertNotIn("password_hash", response.json)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Expires=", cookie)

    def test_form_login(self):
        self.assertEqual(self.login(form=True).status_code, 200)

    def test_login_csrf_missing_wrong_and_unicode(self):
        token = self.client.get("/farmer/login").json["csrf_token"]
        for value in ("", "x" * len(token), "\u00e9" * len(token)):
            self.assertEqual(self.client.post("/farmer/login", json={"csrf_token": value}).status_code, 400)
        self.auth.assert_not_called()

    def test_login_csrf_session_binding_and_expiry(self):
        token = self.client.get("/farmer/login").json["csrf_token"]
        other = self.app.test_client()
        self.assertEqual(other.post("/farmer/login", json={"csrf_token": token}).status_code, 400)
        with self.client.session_transaction() as session:
            session["login_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.post("/farmer/login", json={"csrf_token": token}).status_code, 400)
        self.auth.assert_not_called()

    def test_failed_login_clears_previous_identity(self):
        self.login()
        self.auth.return_value = {"ok": False, "code": "invalid_credentials", "message": "Invalid phone number or password."}
        response = self.login()
        self.assertEqual(response.status_code, 401)
        self.assertIn("csrf_token", response.json)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_login_operational_failure_is_not_invalid_credentials(self):
        self.auth.return_value = {"ok": False, "code": "authentication_error", "message": "Authentication could not be completed. Try again later."}
        self.assertEqual(self.login().status_code, 503)
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_authenticated_home_rechecks_identity(self):
        self.login()
        response = self.client.get("/farmer/home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn("Welcome, farmer.", response.get_data(as_text=True))
        self.assertIn("Farmer dashboard", response.get_data(as_text=True))
        self.lookup.assert_called_once_with(41, 91)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_dashboard_escapes_optional_revalidated_name(self):
        self.login()
        self.lookup.return_value = {"ok": True, "identity": dict(self.identity, first_name="<script>unsafe</script>")}
        response = self.client.get("/farmer/home")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Welcome, &lt;script&gt;unsafe&lt;/script&gt;.", html)
        self.assertNotIn("<script>unsafe</script>", html)

    def test_dashboard_provides_scoped_logout_token_without_identity_details(self):
        token = self.login().json["csrf_token"]
        html = self.client.get("/farmer/home").get_data(as_text=True)
        page = DashboardParser(html)
        self.assertEqual(page.elements["logout-csrf-token"][1]["value"], token)
        self.assertEqual(page.elements["logout-button"][0], "button")
        self.assertEqual(page.elements["logout-button"][1]["type"], "button")
        self.assertTrue(all(token not in url for url in page.urls))
        self.assertIn("/static/javascript/dashboard.js", page.urls)
        for value in ("<form", "user_id", "farmer_id", "password_hash"):
            self.assertNotIn(value, html)

    def dashboard_logout_token(self):
        html = self.client.get("/farmer/home").get_data(as_text=True)
        return DashboardParser(html).elements["logout-csrf-token"][1]["value"]

    def test_dashboard_token_logs_out_and_blocks_subsequent_access(self):
        self.login()
        token = self.dashboard_logout_token()
        response = self.client.post("/farmer/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json["ok"], True)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {})
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_invalid_dashboard_logout_token_preserves_session(self):
        self.login()
        self.dashboard_logout_token()
        response = self.client.post("/farmer/logout", headers={"X-CSRF-Token": "invalid-token"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "csrf_error")
        self.assertEqual(self.client.get("/farmer/home").status_code, 200)

    def test_logout_authorization_failure_does_not_claim_success_or_clear_session(self):
        self.login()
        token = self.dashboard_logout_token()
        self.lookup.return_value = {"ok": False, "code": "authentication_error"}
        response = self.client.post("/farmer/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 503)
        self.assertIs(response.json["ok"], False)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], 41)

    def test_dashboard_asset_is_served(self):
        response = self.client.get("/static/javascript/dashboard.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/farmer/logout", response.get_data(as_text=True))
        response.close()

    def test_unauthenticated_home(self):
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        self.lookup.assert_not_called()

    def test_malformed_sessions_rejected_before_database_lookup(self):
        for changes in ({"user_id": "41"}, {"user_id": True}, {"farmer_id": 0}, {"farmer_id": None}, {"authenticated_at": "now"}, {"authenticated_at": True}, {"authenticated_at": time.time() + 60}):
            with self.subTest(changes=changes):
                with self.client.session_transaction() as session:
                    session.update(self.identity, authenticated_at=time.time())
                    session.update(changes)
                self.assertEqual(self.client.get("/farmer/home").status_code, 401)
                with self.client.session_transaction() as session:
                    self.assertEqual(dict(session), {})
        self.lookup.assert_not_called()

    def test_expired_session(self):
        with self.client.session_transaction() as session:
            session.update(self.identity, authenticated_at=time.time() - 1801)
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        self.lookup.assert_not_called()

    def test_inactive_wrong_role_missing_or_mismatched_profile_invalidates_session(self):
        # The SQL authorization lookup returns no row for each unauthorized case.
        for reason in ("disabled", "pending", "officer", "deleted", "missing-profile", "mismatched-profile"):
            with self.subTest(reason=reason):
                self.login()
                self.lookup.return_value = {"ok": False, "code": "invalid_session"}
                self.assertEqual(self.client.get("/farmer/home").status_code, 401)
                with self.client.session_transaction() as session:
                    self.assertEqual(dict(session), {})

    def test_authorization_outage_fails_closed(self):
        self.login()
        self.lookup.return_value = {"ok": False, "code": "authentication_error"}
        response = self.client.get("/farmer/home")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("identity", response.json)

    def test_tampered_cookie(self):
        self.login()
        cookie = self.client.get_cookie("session").value
        self.client.set_cookie("session", cookie + "tampered")
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        self.lookup.assert_not_called()

    def test_logout_post_with_csrf_clears_session(self):
        token = self.login().json["csrf_token"]
        response = self.client.post("/farmer/logout", data={"csrf_token": token})
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {})
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_logout_requires_post_and_csrf(self):
        token = self.login().json["csrf_token"]
        self.assertEqual(self.client.get("/farmer/logout").status_code, 405)
        for value in ("", "wrong", "\u00e9" * len(token)):
            self.assertEqual(self.client.post("/farmer/logout", json={"csrf_token": value}).status_code, 400)
        self.assertEqual(self.client.get("/farmer/home").status_code, 200)

    def test_logout_accepts_csrf_header(self):
        token = self.login().json["csrf_token"]
        self.assertEqual(self.client.post("/farmer/logout", headers={"X-CSRF-Token": token}).status_code, 200)

    def test_old_login_token_cannot_be_reused_for_logout(self):
        token = self.client.get("/farmer/login").json["csrf_token"]
        self.client.post("/farmer/login", json={"phone_number": "0712345678", "password": "synthetic-password", "csrf_token": token})
        self.assertEqual(self.client.post("/farmer/logout", json={"csrf_token": token}).status_code, 400)

    def test_login_rejects_malformed_requests(self):
        self.assertEqual(self.client.post("/farmer/login", data="text", content_type="text/plain").status_code, 415)
        self.assertEqual(self.client.post("/farmer/login", json=[]).status_code, 400)
        self.auth.assert_not_called()

    def test_absolute_expiry_configuration_and_secure_cookie(self):
        self.assertEqual(self.app.permanent_session_lifetime.total_seconds(), 1800)
        self.assertFalse(self.app.config["SESSION_REFRESH_EACH_REQUEST"])
        self.app.config["SESSION_COOKIE_SECURE"] = True
        response = self.client.get("/farmer/login", base_url="https://localhost")
        self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_real_authorization_lookup_is_parameterized_and_hash_free(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = self.identity
        with patch("app.services.authentication_service.get_db_connection", return_value=connection):
            result = get_active_farmer_identity(41, 91)
        self.assertEqual(result, {"ok": True, "identity": self.identity})
        sql, values = cursor.execute.call_args.args
        self.assertEqual(values, (41, 91))
        self.assertIn("u.role = 'farmer'", sql)
        self.assertIn("u.status = 'active'", sql)
        self.assertIn("f.user_id = u.user_id", sql)
        self.assertNotIn("password_hash", sql)
        connection.commit.assert_not_called()
        cursor.close.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_authorization_lookup_missing_record_and_error(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = None
        with patch("app.services.authentication_service.get_db_connection", return_value=connection):
            self.assertEqual(get_active_farmer_identity(41, 91)["code"], "invalid_session")
            cursor.execute.side_effect = Error("private-password-hash")
            with self.assertLogs("app.services.authentication_service", level="ERROR") as logs:
                result = get_active_farmer_identity(41, 91)
        self.assertEqual(result["code"], "authentication_error")
        self.assertNotIn("private-password-hash", str(result) + str(logs.output))


if __name__ == "__main__":
    unittest.main()
