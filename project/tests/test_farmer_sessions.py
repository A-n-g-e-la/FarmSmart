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
        self.links = []
        self.buttons = []
        self.sections = []
        self.headings = []
        self.ids = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements[attrs.get("id")] = (tag, attrs)
        self.urls.extend(attrs[key] for key in ("href", "src", "action") if key in attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "a":
            self.links.append(attrs)
        if tag == "button":
            self.buttons.append(attrs)
        if tag == "section":
            self.sections.append(attrs)
        if tag in ("h1", "h2"):
            self.headings.append(tag)


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
        for value in ("user_id", "farmer_id", "password_hash"):
            self.assertNotIn(value, html)

    def dashboard_logout_token(self):
        html = self.client.get("/farmer/home").get_data(as_text=True)
        return DashboardParser(html).elements["logout-csrf-token"][1]["value"]

    def test_dashboard_sections_have_accessible_labels_and_honest_empty_states(self):
        self.login()
        html = self.client.get("/farmer/home").get_data(as_text=True)
        page = DashboardParser(html)
        self.assertEqual(page.headings.count("h1"), 1)
        self.assertEqual(len(page.ids), len(set(page.ids)))
        expected_sections = {
            "overview", "quick-actions", "farm-plots", "yield",
            "finances", "fertilizer", "history",
        }
        self.assertEqual({section["id"] for section in page.sections}, expected_sections)
        for section in page.sections:
            heading = page.elements[section["aria-labelledby"]]
            self.assertIn(heading[0], ("h1", "h2"))
        for text in (
            "Enable JavaScript to load your farm plots.", "No predictions yet",
            "No recommendations yet", "No prediction history yet",
            "Production costs", "Revenue", "Profit", "Return on investment (ROI)",
            "Not available yet", "Coming later: request a prediction",
        ):
            self.assertIn(text, html)
        # Acreage input limits are guidance; unavailable predictions/finances stay empty.
        self.assertNotRegex(html, r"\d[\d,.]*\s*(?:kg|bags?|%|KES)")
        self.assertNotIn("<canvas", html)
        self.assertNotIn("chart.js", html.lower())

    def test_dashboard_navigation_resolves_without_unimplemented_actions(self):
        self.login()
        page = DashboardParser(self.client.get("/farmer/home").get_data(as_text=True))
        for link in page.links:
            href = link["href"]
            if href == "/farmer/login-page":
                continue
            self.assertTrue(href.startswith("#"), href)
            self.assertIn(href[1:], page.elements)
            # Fragment targets can receive keyboard focus after navigation.
            self.assertEqual(page.elements[href[1:]][1].get("tabindex"), "-1")
        self.assertEqual([button["id"] for button in page.buttons], [
            "logout-button", "edit-plot", "save-plot-changes", "cancel-plot-edit",
            "toggle-add-plot", "refresh-plots", "save-plot",
        ])
        self.assertIn("disabled", page.buttons[0])

    def test_plot_form_matches_backend_contract_and_has_free_text_placeholder(self):
        self.login()
        html = self.client.get("/farmer/home").get_data(as_text=True)
        page = DashboardParser(html)
        self.assertEqual(page.elements["plot-form"][1]["action"], "/farmer/plots")
        self.assertEqual(page.elements["plot-form"][1]["method"], "post")
        for element_id, name, limit in (("plot_name", "plot_name", "100"), ("area_acres", "area_acres", "32"),
                                        ("plot_location", "location", "150"), ("soil_type", "soil_type", "100"),
                                        ("farming_type", "farming_type", "100")):
            tag, attrs = page.elements[element_id]
            self.assertEqual(tag, "input")
            self.assertEqual(attrs["name"], name)
            self.assertEqual(attrs["maxlength"], limit)
            self.assertEqual("required" in attrs, name in ("plot_name", "area_acres"))
            self.assertIn(f'for="{element_id}"', html)
            self.assertIn(name + "-error", attrs["aria-describedby"])
        tag, farming = page.elements["farming_type"]
        self.assertEqual(farming["type"], "text")
        self.assertEqual(farming["placeholder"], "e.g., Rain-fed or Irrigated")
        self.assertNotIn("value", farming)
        self.assertNotIn("pattern", farming)
        self.assertIn("disabled", page.elements["plot-fields"][1])
        self.assertIn('Area (acres) (required)', html)
        self.assertIn('0.001 to 999999.999 acres', html)

    def test_add_plot_disclosure_is_collapsed_and_success_feedback_stays_outside(self):
        self.login()
        html = self.client.get("/farmer/home").get_data(as_text=True)
        page = DashboardParser(html)
        tag, toggle = page.elements["toggle-add-plot"]
        self.assertEqual(tag, "button")
        self.assertEqual(toggle["type"], "button")
        self.assertEqual(toggle["aria-expanded"], "false")
        self.assertEqual(toggle["aria-controls"], "add-plot")
        self.assertIn("+ Add Farm Plot", html)
        self.assertIn("hidden", page.elements["add-plot"][1])
        self.assertEqual(page.elements["add-plot"][1]["tabindex"], "-1")
        self.assertEqual(page.elements["quick-add-plot"][1]["href"], "#add-plot")
        self.assertEqual(page.elements["plot-form-status"][1]["role"], "status")
        self.assertLess(html.index('id="plot-form-status"'), html.index('id="add-plot"'))

    def test_plot_selection_has_labeled_details_and_no_default_selection(self):
        self.login()
        html = self.client.get("/farmer/home").get_data(as_text=True)
        page = DashboardParser(html)
        self.assertIn("Selected plot details", html)
        self.assertIn("Select a farm plot to view its details.", html)
        self.assertIn("hidden", page.elements["selected-plot-details"][1])
        self.assertEqual(page.elements["selected-plot-details"][0], "dl")
        self.assertEqual(page.elements["plot-selection-status"][1]["role"], "status")
        self.assertEqual(page.elements["plot-selection-status"][1]["aria-live"], "polite")

    def test_edit_form_is_hidden_accessible_and_has_independent_fields(self):
        self.login()
        html = self.client.get("/farmer/home").get_data(as_text=True)
        page = DashboardParser(html)
        self.assertIn("hidden", page.elements["edit-plot"][1])
        self.assertIn("hidden", page.elements["edit-plot-panel"][1])
        self.assertIn("disabled", page.elements["edit-plot-fields"][1])
        self.assertEqual(page.elements["edit-plot"][1]["aria-expanded"], "false")
        self.assertEqual(page.elements["edit-plot"][1]["aria-controls"], "edit-plot-panel")
        self.assertEqual(page.elements["edit-plot-form"][1]["aria-labelledby"], "edit-plot-title")
        self.assertEqual(page.elements["save-plot-changes"][1]["type"], "submit")
        self.assertEqual(page.elements["cancel-plot-edit"][1]["type"], "button")
        for name, limit in (("plot_name", "100"), ("area_acres", "32"), ("location", "150"),
                            ("soil_type", "100"), ("farming_type", "100")):
            attrs = page.elements["edit-" + name][1]
            self.assertEqual(attrs["name"], name)
            self.assertEqual(attrs["maxlength"], limit)
            self.assertEqual("required" in attrs, name in ("plot_name", "area_acres"))
            self.assertIn(f'for="edit-{name}"', html)
            self.assertIn("edit-" + name + "-error", attrs["aria-describedby"])
        self.assertEqual(page.elements["edit-farming_type"][1]["placeholder"], "e.g., Rain-fed or Irrigated")
        self.assertEqual(page.elements["edit-farming_type"][1]["type"], "text")
        self.assertEqual(page.elements["edit-plot-status"][1]["role"], "status")

    def test_dashboard_assets_are_local_and_served(self):
        self.login()
        page = DashboardParser(self.client.get("/farmer/home").get_data(as_text=True))
        assets = [url for url in page.urls if not url.startswith("#")]
        self.assertIn("/static/css/dashboard.css", assets)
        for url in assets:
            if url == "/farmer/plots":
                continue  # Form POST action, not a static asset.
            self.assertTrue(url.startswith("/static/") or url == "/farmer/login-page", url)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()

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
