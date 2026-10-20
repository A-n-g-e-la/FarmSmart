import time
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

import dotenv

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app


class AuthPageParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.labels = set()
        self.elements = {}
        self.assets = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.inputs[attrs["name"]] = attrs
        if tag == "label":
            self.labels.add(attrs["for"])
        if "id" in attrs:
            self.elements[attrs["id"]] = attrs
        if tag in ("link", "script"):
            self.assets.append(attrs.get("href", attrs.get("src")))


class OfficerAuthenticationWebTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-officer-auth-web-key")
        self.client = self.app.test_client()
        self.identity = {"user_id": 41, "agricultural_officer_id": 91}

    def tearDown(self):
        self.guard.assert_not_called()

    def sign_in(self):
        with self.client.session_transaction() as session:
            session.update(self.identity, authenticated_at=time.time())

    def test_login_page_is_public_accessible_and_reuses_branding_without_database_access(self):
        response = self.client.get("/officer/login-page?email=private-email&password=private-password")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        html = response.get_data(as_text=True)
        page = AuthPageParser(html)
        self.assertIn("FarmAdvis", html)
        self.assertIn('name="viewport"', html)
        self.assertEqual(set(page.inputs), {"email", "password"})
        self.assertEqual(page.labels, {"email", "password"})
        for name, attrs in page.inputs.items():
            self.assertIn("required", attrs)
            self.assertEqual(attrs["aria-describedby"], name + "-error")
            self.assertNotIn("value", attrs)
        self.assertEqual(page.inputs["email"]["maxlength"], "254")
        self.assertEqual(page.inputs["password"]["maxlength"], "1024")
        self.assertEqual(page.inputs["password"]["autocomplete"], "current-password")
        self.assertEqual(page.inputs["password"]["type"], "password")
        self.assertEqual(page.elements["officer-password-toggle"]["aria-controls"], "password")
        self.assertEqual(page.elements["officer-login-form"]["action"], "/officer/login")
        self.assertIn("disabled", page.elements["officer-login-button"])
        self.assertEqual(page.elements["officer-login-error"]["role"], "alert")
        self.assertIn('href="/officer/register-page"', html)
        self.assertNotIn("private-email", html)
        self.assertNotIn("private-password", html)
        for asset in page.assets:
            with self.client.get(asset) as asset_response:
                self.assertEqual(asset_response.status_code, 200)

    def test_dashboard_is_protected_and_exposes_read_only_farmer_and_plot_panels(self):
        self.sign_in()
        with patch("app.security.get_active_officer_identity", return_value={"ok": True, "identity": self.identity}) as lookup:
            response = self.client.get("/officer/dashboard")
        lookup.assert_called_once_with(41, 91)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        html = response.get_data(as_text=True)
        self.assertIn("Welcome, agricultural officer.", html)
        page = AuthPageParser(html)
        self.assertEqual(page.elements["officer-logout-button"]["type"], "button")
        self.assertIn("disabled", page.elements["officer-logout-button"])
        self.assertEqual(page.elements["officer-logout-error"]["role"], "alert")
        self.assertEqual(page.inputs, {})
        self.assertIn("Officer Dashboard | FarmAdvis", html)
        self.assertIn("Total assigned farmers", html)
        self.assertIn("Total registered plots", html)
        for identifier in ("assigned-count", "assigned-plot-count", "assigned-farmers", "farmer-plots", "selected-farmer"):
            self.assertIn(identifier, page.elements)
        for identifier in ("farmers-refresh", "plots-retry", "officer-logout-button"):
            self.assertEqual(page.elements[identifier]["type"], "button")
        for identifier in ("farmers-status", "farmer-plots-status"):
            self.assertEqual(page.elements[identifier]["role"], "status")
        self.assertIn("/static/javascript/officer-dashboard.js", page.assets)
        self.assertIn("/static/css/officer-dashboard.css", page.assets)
        self.assertIn("/static/javascript/officer-auth.js", page.assets)
        self.assertNotIn("<form", html)
        for action in ("Edit Plot", "Delete Plot", "Add Farm Plot", "Assign farmer"):
            self.assertNotIn(action, html)
        for forbidden in ("user_id", "agricultural_officer_id", "password_hash", "/farmer/", "csrf_token", "<canvas"):
            self.assertNotIn(forbidden, html)
        for asset in page.assets:
            with self.client.get(asset) as asset_response:
                self.assertEqual(asset_response.status_code, 200)

    def test_anonymous_farmer_and_expired_sessions_cannot_render_landing(self):
        for identity in ({}, {"user_id": 51, "farmer_id": 101, "authenticated_at": time.time()},
                         dict(self.identity, authenticated_at=time.time() - 1801)):
            with self.subTest(identity=identity):
                with self.client.session_transaction() as session:
                    session.clear()
                    session.update(identity)
                with patch("app.security.get_active_officer_identity") as lookup:
                    response = self.client.get("/officer/dashboard")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("Welcome, agricultural officer.", response.get_data(as_text=True))
                lookup.assert_not_called()

    def test_pending_disabled_unapproved_and_missing_profiles_cannot_render_landing(self):
        # Actual revalidation SQL returns no row for any revoked condition.
        for reason in ("pending", "disabled", "unapproved", "missing_profile", "farmer"):
            with self.subTest(reason=reason):
                self.sign_in()
                connection = MagicMock()
                connection.cursor.return_value.fetchone.return_value = None
                with patch("app.services.officer_authentication_service.get_db_connection", return_value=connection):
                    response = self.client.get("/officer/dashboard")
                self.assertEqual(response.status_code, 401)
                sql = connection.cursor.return_value.execute.call_args.args[0]
                self.assertIn("u.role = 'officer'", sql)
                self.assertIn("u.status = 'active'", sql)
                self.assertIn("o.approved_at IS NOT NULL", sql)
                with self.client.session_transaction() as session:
                    self.assertFalse(dict(session))

    def test_revalidation_outage_denies_landing_safely(self):
        self.sign_in()
        with patch("app.security.get_active_officer_identity", return_value={"ok": False, "code": "authentication_error"}):
            response = self.client.get("/officer/dashboard")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Welcome, agricultural officer.", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_mocked_login_landing_token_and_logout_flow(self):
        self.assertEqual(self.client.get("/officer/login-page").status_code, 200)
        token = self.client.get("/officer/login").json["csrf_token"]
        with patch("app.routes.officer.authenticate_officer", return_value={"ok": True, "identity": self.identity}) as auth:
            response = self.client.post("/officer/login", json={"email": "officer@example.com", "password": "synthetic-password"}, headers={"X-CSRF-Token": token})
        auth.assert_called_once_with("officer@example.com", "synthetic-password")
        self.assertEqual(response.status_code, 200)
        with patch("app.security.get_active_officer_identity", return_value={"ok": True, "identity": self.identity}):
            self.assertEqual(self.client.get("/officer/dashboard").status_code, 200)
            logout_token = self.client.get("/officer/home").json["csrf_token"]
            self.assertEqual(self.client.post("/officer/logout", headers={"X-CSRF-Token": "wrong"}).status_code, 400)
            self.assertEqual(self.client.post("/officer/logout", headers={"X-CSRF-Token": logout_token}).status_code, 200)
        self.assertEqual(self.client.get("/officer/dashboard").status_code, 401)
        self.assertEqual(self.client.get("/officer/login-page").status_code, 200)

    def test_officer_session_cannot_render_farmer_home(self):
        self.sign_in()
        with patch("app.security.get_active_farmer_identity") as lookup:
            self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
