import unittest
from html.parser import HTMLParser
from unittest.mock import patch

import dotenv

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app


class PageParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.inputs = {}
        self.labels = set()
        self.elements = {}
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input":
            self.inputs[attrs["name"]] = attrs
        if tag == "label":
            self.labels.add(attrs["for"])
        if "id" in attrs:
            self.elements[attrs["id"]] = attrs
        if tag in ("a", "link", "script"):
            self.links.append(attrs.get("href", attrs.get("src")))


class OfficerRegistrationWebTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-officer-web-test-key")
        self.client = self.app.test_client()

    def tearDown(self):
        self.live_connector.assert_not_called()

    def test_page_renders_accessible_fields_and_backend_limits_without_database_access(self):
        with patch("app.routes.officer.register_officer") as registration:
            response = self.client.get("/officer/register-page")
        registration.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.mimetype, "text/html")
        html = response.get_data(as_text=True)
        page = PageParser(html)
        required = {"first_name", "last_name", "email", "password", "password_confirmation", "region"}
        self.assertEqual(set(page.inputs), required | {"organization"})
        self.assertEqual(page.labels, set(page.inputs))
        for name, attrs in page.inputs.items():
            self.assertEqual("required" in attrs, name in required)
            self.assertIn(name + "-error", attrs["aria-describedby"])
            self.assertNotIn("value", attrs)
            self.assertEqual(attrs["maxlength"], str({
                "email": 254, "organization": 150, "password": 1024, "password_confirmation": 1024,
            }.get(name, 100)))
        self.assertEqual(page.inputs["email"]["type"], "email")
        for name in ("password", "password_confirmation"):
            self.assertEqual(page.inputs[name]["type"], "password")
            self.assertEqual(page.inputs[name]["autocomplete"], "new-password")
            self.assertEqual(page.elements[name + "-toggle"]["aria-controls"], name)
        self.assertIn("(optional)", html)
        self.assertIn('name="viewport"', html)
        self.assertIn("Enable JavaScript", html)
        self.assertEqual(page.elements["officer-registration-form"]["action"], "/officer/register")
        self.assertIn("disabled", page.elements["officer-submit"])
        self.assertIn("hidden", page.elements["officer-confirmation"])
        self.assertEqual(page.elements["officer-error"]["role"], "alert")
        self.assertEqual(page.elements["officer-confirmation"]["tabindex"], "-1")
        self.assertIn("pending administrator approval", html)
        self.assertIn("You have not been logged in", html)
        with self.client.session_transaction() as session:
            self.assertFalse(dict(session))

    def test_all_page_assets_and_links_exist_without_officer_login_or_dashboard_links(self):
        html = self.client.get("/officer/register-page").get_data(as_text=True)
        page = PageParser(html)
        for url in page.links:
            with self.subTest(url=url):
                with self.client.get(url) as response:
                    self.assertEqual(response.status_code, 200)
        self.assertIn("/static/css/registration.css", page.links)
        self.assertIn("/static/css/officer-registration.css", page.links)
        self.assertIn("/static/javascript/officer-registration.js", page.links)
        self.assertNotIn("/officer/login", html)
        self.assertNotIn("/officer/home", html)

    def test_page_token_and_json_submission_contract_without_automatic_login(self):
        self.assertEqual(self.client.get("/officer/register-page").status_code, 200)
        token = self.client.get("/officer/register").json["csrf_token"]
        data = {"first_name": "Test", "last_name": "Officer", "email": "officer@example.com",
                "password": "synthetic-password", "password_confirmation": "synthetic-password",
                "region": "Uasin Gishu", "organization": ""}
        with patch("app.routes.officer.register_officer", return_value={
            "ok": True, "status": "pending", "message": "Approval required.",
        }) as registration:
            response = self.client.post("/officer/register", json=data, headers={"X-CSRF-Token": token})
        registration.assert_called_once_with(data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["status"], "pending")
        self.assertNotIn("Location", response.headers)
        with self.client.session_transaction() as session:
            for key in ("user_id", "farmer_id", "agricultural_officer_id", "authenticated_at", "officer_registration_csrf"):
                self.assertNotIn(key, session)
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)

    def test_page_does_not_reflect_query_values_or_change_existing_farmer_session(self):
        with self.client.session_transaction() as session:
            session.update(user_id=41, farmer_id=91, logout_csrf="synthetic-existing-token")
        response = self.client.get("/officer/register-page?password=private-password&first_name=private-name")
        html = response.get_data(as_text=True)
        self.assertNotIn("private-password", html)
        self.assertNotIn("private-name", html)
        self.assertNotIn("synthetic-existing-token", html)
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {"user_id": 41, "farmer_id": 91, "logout_csrf": "synthetic-existing-token"})


if __name__ == "__main__":
    unittest.main()
