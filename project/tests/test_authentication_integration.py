import time
import unittest
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error
from werkzeug.security import generate_password_hash

# These integration tests exercise Flask, real templates, and services with fake SQL.
with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app


class AuthenticationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "synthetic-integration-password"
        cls.password_hash = generate_password_hash(cls.password, method="scrypt")

    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-integration-session-key")
        self.client = self.app.test_client()
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = {
            "user_id": 41, "farmer_id": 91, "role": "farmer",
            "status": "active", "password_hash": self.password_hash,
        }
        helper = patch("app.services.authentication_service.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)
        # A missing mock must fail rather than connect to the development database.
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)

    def token(self):
        response = self.client.get("/farmer/login")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        self.assertEqual(set(response.json), {"ok", "csrf_token"})
        return response.json["csrf_token"]

    def login(self, password=None):
        # Match login.js: JSON credentials, header token, and a shared cookie session.
        return self.client.post("/farmer/login", json={
            "phone_number": "0712 345 678",
            "password": self.password if password is None else password,
        }, headers={"X-CSRF-Token": self.token()})

    def test_login_page_renders_without_authentication(self):
        response = self.client.get("/farmer/login-page")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn('id="login-form"', response.get_data(as_text=True))
        self.assertIn('action="/farmer/login"', response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.post("/farmer/login-page").status_code, 405)
        self.helper.assert_not_called()

    def test_registration_to_login_navigation(self):
        response = self.client.get("/farmer/registration")
        self.assertEqual(response.status_code, 200)
        self.assertIn('href="/farmer/login-page"', response.get_data(as_text=True))
        self.assertEqual(self.client.get("/farmer/login-page").status_code, 200)
        self.helper.assert_not_called()

    def test_login_to_registration_navigation(self):
        response = self.client.get("/farmer/login-page")
        self.assertIn('href="/farmer/registration"', response.get_data(as_text=True))
        self.assertEqual(self.client.get("/farmer/registration").status_code, 200)
        self.helper.assert_not_called()

    def test_html_route_does_not_replace_token_api(self):
        self.client.get("/farmer/login-page")
        token = self.token()
        self.assertTrue(token)
        with self.client.session_transaction() as session:
            self.assertEqual(session["login_csrf"], token)
            self.assertNotIn("user_id", session)
        self.helper.assert_not_called()

    def test_login_home_logout_flow(self):
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        self.client.get("/farmer/login-page")
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["identity"], {"user_id": 41, "farmer_id": 91})
        self.assertEqual(set(response.json), {"ok", "identity", "csrf_token"})
        self.assertEqual(self.cursor.execute.call_args.args[1], ("+254712345678",))
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], 41)
            self.assertEqual(session["farmer_id"], 91)
            self.assertNotIn(self.password, str(dict(session)))
            self.assertNotIn(self.password_hash, str(dict(session)))
        home = self.client.get("/farmer/home")
        self.assertEqual(home.status_code, 200)
        self.assertEqual(home.mimetype, "text/html")
        self.assertIn("Welcome, farmer.", home.get_data(as_text=True))
        # Login still supplies the logout token; the HTML dashboard has no logout UI yet.
        logout = self.client.post("/farmer/logout", headers={"X-CSRF-Token": response.json["csrf_token"]})
        self.assertEqual(logout.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {})
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        self.connection.commit.assert_not_called()
        self.guard.assert_not_called()

    def test_invalid_credentials_create_no_authenticated_session(self):
        response = self.login(password="wrong-password")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json["code"], "invalid_credentials")
        self.assertNotIn(self.password_hash, str(response.json))
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_login_csrf_rejection_and_expiry(self):
        token = self.token()
        self.assertEqual(self.client.post("/farmer/login", json={"phone_number": "0712345678", "password": self.password}).status_code, 400)
        with self.client.session_transaction() as session:
            session["login_csrf_issued"] = time.time() - 3601
        response = self.client.post("/farmer/login", json={"csrf_token": token})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["code"], "csrf_error")
        self.helper.assert_not_called()

    def test_logout_requires_csrf_and_post(self):
        self.login()
        self.assertEqual(self.client.get("/farmer/logout").status_code, 405)
        self.assertEqual(self.client.post("/farmer/logout").status_code, 400)
        self.assertEqual(self.client.get("/farmer/home").status_code, 200)

    def test_operational_error_preserves_safe_api_contract(self):
        self.helper.side_effect = Error("private-database-details")
        with self.assertLogs("app.services.authentication_service", level="ERROR"):
            response = self.login()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "authentication_error")
        self.assertNotIn("private-database-details", str(response.json))
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_login_assets_served_from_integrated_checkout(self):
        html = self.client.get("/farmer/login-page").get_data(as_text=True)
        for name in ("css/registration.css", "css/login.css", "javascript/login.js"):
            self.assertIn("/static/" + name, html)
            response = self.client.get("/static/" + name)
            self.assertEqual(response.status_code, 200)
            response.close()
        self.helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
