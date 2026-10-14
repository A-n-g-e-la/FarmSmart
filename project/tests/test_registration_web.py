import time
import unittest
from html.parser import HTMLParser
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

# Keep imports and every test isolated from local credentials and the real database.
with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app


class InputParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            values = dict(attrs)
            self.inputs[values.get("name")] = values


class RegistrationWebTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-test-session-key")
        self.client = self.app.test_client()
        self.data = {
            "first_name": "Amina", "last_name": "Kiptoo",
            "phone_number": "0712345678", "location": "Moiben",
            "password": "synthetic-password-123",
            "password_confirmation": "synthetic-password-123",
        }
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = None

        def execute(sql, parameters):
            self.cursor.lastrowid = 72 if sql.startswith("INSERT INTO farmers") else 35

        self.cursor.execute.side_effect = execute
        helper = patch("app.services.registration_service.get_db_connection", return_value=self.connection)
        self.mock_helper = helper.start()
        self.addCleanup(helper.stop)
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database access forbidden"))
        self.live_guard = guard.start()
        self.addCleanup(guard.stop)

    def get_token(self, client=None):
        response = (client or self.client).get("/farmer/registration")
        self.assertEqual(response.status_code, 200)
        return InputParser(response.get_data(as_text=True)).inputs["csrf_token"]["value"]

    def submit(self, changes=None, follow_redirects=False):
        data = dict(self.data, csrf_token=self.get_token())
        data.update(changes or {})
        return self.client.post("/farmer/registration", data=data, follow_redirects=follow_redirects)

    def assert_passwords_empty(self, response):
        html = response.get_data(as_text=True)
        inputs = InputParser(html).inputs
        for name in ("password", "password_confirmation"):
            self.assertEqual(inputs[name]["type"], "password")
            self.assertNotIn("value", inputs[name])
        self.assertNotIn(self.data["password"], html)

    def test_get_form(self):
        response = self.client.get("/farmer/registration")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        for name in self.data:
            self.assertIn('for="' + name + '"', html)
        self.assertIn('method="post"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('hidden>Show</button>', html)
        self.assert_passwords_empty(response)
        self.mock_helper.assert_not_called()
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_success_uses_service_transaction_and_prg(self):
        old_token = self.get_token()
        response = self.client.post("/farmer/registration", data=dict(self.data, csrf_token=old_token))
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/farmer/registration")
        self.connection.commit.assert_called_once_with()
        user_values = self.cursor.execute.call_args_list[1].args[1]
        self.assertEqual(user_values[2], "+254712345678")
        self.assertTrue(user_values[3].startswith("scrypt:"))
        self.assertEqual(self.cursor.execute.call_args_list[2].args[1], (35, "Moiben"))
        success = self.client.get(response.headers["Location"])
        self.assertIn("account was created successfully", success.get_data(as_text=True))
        self.assertIn("not been logged in", success.get_data(as_text=True))
        self.assert_passwords_empty(success)
        new_token = InputParser(success.get_data(as_text=True)).inputs["csrf_token"]["value"]
        self.assertNotEqual(old_token, new_token)
        self.client.get("/farmer/registration")
        self.connection.commit.assert_called_once_with()
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)
            self.assertNotIn("farmer_id", session)
            self.assertNotIn(self.data["password"], str(dict(session)))

    def test_invalid_fields_and_safe_values_preserved(self):
        response = self.submit({"phone_number": "invalid", "password_confirmation": "different"})
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn('id="phone_number-error"', html)
        self.assertIn('id="password_confirmation-error"', html)
        inputs = InputParser(html).inputs
        for name in ("first_name", "last_name", "location"):
            self.assertEqual(inputs[name]["value"], self.data[name])
        self.assertEqual(inputs["phone_number"]["value"], "invalid")
        self.assertEqual(inputs["phone_number"]["aria-invalid"], "true")
        self.assert_passwords_empty(response)
        self.mock_helper.assert_not_called()

    def test_required_fields(self):
        response = self.submit({"first_name": "", "last_name": "", "password": "", "password_confirmation": ""})
        self.assertEqual(response.status_code, 400)
        for name in ("first_name", "last_name", "password", "password_confirmation"):
            self.assertIn('id="' + name + '-error"', response.get_data(as_text=True))
        self.mock_helper.assert_not_called()

    def test_duplicate_phone(self):
        self.cursor.fetchone.return_value = (35,)
        response = self.submit()
        self.assertEqual(response.status_code, 409)
        self.assertIn("already registered", response.get_data(as_text=True))
        self.assert_passwords_empty(response)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_missing_csrf(self):
        response = self.client.post("/farmer/registration", data=self.data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("could not be verified", response.get_data(as_text=True))
        self.assert_passwords_empty(response)
        self.mock_helper.assert_not_called()

    def test_invalid_and_unicode_csrf(self):
        token = self.get_token()
        for bad in ("wrong-token", "x" * len(token), "\u00e9" * len(token)):
            with self.subTest(token_type=type(bad).__name__):
                response = self.client.post("/farmer/registration", data=dict(self.data, csrf_token=bad))
                self.assertEqual(response.status_code, 400)
        self.mock_helper.assert_not_called()

    def test_token_bound_to_browser_session(self):
        token = self.get_token()
        other = self.app.test_client()
        self.get_token(other)
        response = other.post("/farmer/registration", data=dict(self.data, csrf_token=token))
        self.assertEqual(response.status_code, 400)
        self.mock_helper.assert_not_called()

    def test_expired_csrf(self):
        token = self.get_token()
        with self.client.session_transaction() as session:
            session["registration_csrf_issued"] = time.time() - 3601
        response = self.client.post("/farmer/registration", data=dict(self.data, csrf_token=token))
        self.assertEqual(response.status_code, 400)
        replacement = InputParser(response.get_data(as_text=True)).inputs["csrf_token"]["value"]
        self.assertNotEqual(token, replacement)
        self.mock_helper.assert_not_called()

    def test_consumed_token_rejected(self):
        token = self.get_token()
        data = dict(self.data, csrf_token=token)
        self.assertEqual(self.client.post("/farmer/registration", data=data).status_code, 303)
        self.assertEqual(self.client.post("/farmer/registration", data=data).status_code, 400)
        self.connection.commit.assert_called_once_with()

    def test_user_content_is_escaped(self):
        malicious = '<script>alert("x")</script>'
        response = self.submit({"first_name": malicious, "phone_number": "invalid"})
        html = response.get_data(as_text=True)
        self.assertNotIn(malicious, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertEqual(InputParser(html).inputs["first_name"]["value"], malicious)

    def test_database_error_is_safe(self):
        self.mock_helper.side_effect = Error("private-credentials")
        with self.assertLogs("app.services.registration_service", level="ERROR"):
            response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertIn("Try again later", response.get_data(as_text=True))
        self.assertNotIn("private-credentials", response.get_data(as_text=True))
        self.assert_passwords_empty(response)

    def test_internal_error_status(self):
        self.mock_helper.side_effect = RuntimeError("private-detail")
        with self.assertLogs("app.services.registration_service", level="ERROR"):
            response = self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-detail", response.get_data(as_text=True))

    def test_json_api_still_accepts_no_csrf_and_returns_json(self):
        response = self.client.post("/farmer/register", json=self.data)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.is_json)
        self.assertEqual(response.json["user_id"], 35)
        self.assertEqual(response.json["farmer_id"], 72)
        self.assertEqual(self.client.post("/farmer/register", data=self.data).status_code, 415)

    def test_static_assets(self):
        for name in ("css/registration.css", "javascript/registration.js"):
            response = self.client.get("/static/" + name)
            self.assertEqual(response.status_code, 200)
            response.close()


if __name__ == "__main__":
    unittest.main()
