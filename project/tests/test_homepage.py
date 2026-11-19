import unittest
from html.parser import HTMLParser
from unittest.mock import patch

import dotenv
from flask import url_for

# Import the application without loading workstation credentials.
with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app


class Element:
    def __init__(self, tag, attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.content = []

    @property
    def text(self):
        content = " ".join(item.text if isinstance(item, Element) else item for item in self.content)
        return " ".join(content.split())

    def find_all(self, tag):
        found = []
        for item in self.content:
            if isinstance(item, Element):
                if item.tag == tag:
                    found.append(item)
                found.extend(item.find_all(tag))
        return found


class HomepageParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__()
        self.root = Element("document")
        self.stack = [self.root]
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs)
        self.elements.append(element)
        self.stack[-1].content.append(element)
        if tag not in self.VOID_TAGS:
            self.stack.append(element)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].content.append(data.strip())

    def by_id(self, identifier):
        return next(element for element in self.elements if element.attrs.get("id") == identifier)


class HomepageTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.connector = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(
            TESTING=True, SECRET_KEY="isolated-homepage-test-key",
            MYSQL_PASSWORD="private-database-password", MYSQL_USER="private-database-user",
        )
        self.client = self.app.test_client()

    def tearDown(self):
        # A caught connector error must also fail the test, not silently pass.
        self.connector.assert_not_called()

    def page(self, client=None, path="/"):
        response = (client or self.client).get(path)
        self.assertEqual(response.status_code, 200)
        return response, HomepageParser(response.get_data(as_text=True))

    def test_public_home_is_registered_once_and_renders_without_authentication_or_database(self):
        response, page = self.page()
        self.assertEqual(response.mimetype, "text/html")
        self.assertNotIn("Set-Cookie", response.headers)
        with self.client.session_transaction() as session:
            self.assertFalse(dict(session))
        rules = [rule for rule in self.app.url_map.iter_rules() if rule.rule == "/"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].endpoint, "public.home")
        with self.app.test_request_context():
            self.assertEqual(url_for("public.home"), "/")
        self.assertIn("FarmAdvis", page.root.text)
        self.assertEqual(page.by_id("hero-title").text, "Smarter Farming. Better Decisions.")
        self.assertIn("Uasin Gishu, Kenya", page.root.text)

    def test_all_four_account_links_use_existing_html_endpoints_and_resolve(self):
        _, page = self.page()
        expected = {
            "farmer.login_page": ("/farmer/login-page", "Farmer login"),
            "farmer.registration_form": ("/farmer/registration", "Register as a farmer"),
            "officer.login_page": ("/officer/login-page", "Officer login"),
            "officer.registration_page": ("/officer/register-page", "Register as an officer"),
        }
        anchors = page.root.find_all("a")
        for endpoint, (path, label) in expected.items():
            with self.subTest(endpoint=endpoint), self.app.test_request_context():
                self.assertEqual(url_for(endpoint), path)
                links = [link for link in anchors if link.attrs.get("href") == path]
                self.assertEqual(len(links), 1)
                self.assertIn(label, links[0].text)
                with self.client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, "text/html")
        for api_path in ("/farmer/login", "/farmer/register", "/officer/login", "/officer/register"):
            self.assertNotIn(api_path, [link.attrs.get("href") for link in anchors])

    def test_sections_landmarks_and_heading_hierarchy_are_accessible(self):
        _, page = self.page()
        self.assertEqual(len(page.root.find_all("header")), 1)
        self.assertEqual(len(page.root.find_all("main")), 1)
        self.assertEqual(len(page.root.find_all("footer")), 1)
        self.assertEqual(page.root.find_all("html")[0].attrs["lang"], "en")
        identifiers = [element.attrs["id"] for element in page.elements if "id" in element.attrs]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for identifier in ("features", "about", "get-started"):
            section = page.by_id(identifier)
            self.assertEqual(section.tag, "section")
            heading = page.by_id(section.attrs["aria-labelledby"])
            self.assertEqual(heading.tag, "h2")
            self.assertTrue(heading.text)
        headings = [element for element in page.elements if element.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}]
        self.assertEqual(sum(heading.tag == "h1" for heading in headings), 1)
        self.assertEqual(headings[0].tag, "h1")
        for previous, current in zip(headings, headings[1:]):
            self.assertLessEqual(int(current.tag[1]), int(previous.tag[1]) + 1)
        self.assertEqual({nav.attrs.get("aria-label") for nav in page.root.find_all("nav")}, {"Main navigation", "Footer navigation"})
        viewport = next(element for element in page.root.find_all("meta") if element.attrs.get("name") == "viewport")
        self.assertEqual(viewport.attrs["content"], "width=device-width, initial-scale=1")

    def test_skip_link_has_focusable_main_target_and_all_links_are_keyboard_accessible(self):
        _, page = self.page()
        anchors = page.root.find_all("a")
        self.assertEqual(anchors[0].text, "Skip to content")
        self.assertEqual(anchors[0].attrs["href"], "#main-content")
        main = page.by_id("main-content")
        self.assertEqual(main.tag, "main")
        self.assertEqual(main.attrs["tabindex"], "-1")
        for anchor in anchors:
            href = anchor.attrs["href"]
            self.assertTrue(href)
            self.assertTrue(anchor.attrs.get("aria-label") or anchor.text)
            self.assertNotIn(anchor.text.lower(), ("click here", "here", "more"))
            self.assertNotEqual(anchor.attrs.get("tabindex"), "-1")
            self.assertNotIn("onclick", anchor.attrs)
            if href.startswith("#"):
                self.assertTrue(page.by_id(href[1:]))

    def test_planned_features_are_explicitly_unavailable_and_separate_from_current_features(self):
        _, page = self.page()
        planned = next(element for element in page.elements if element.attrs.get("aria-labelledby") == "planned-title")
        current = next(element for element in page.elements if element.attrs.get("aria-labelledby") == "available-title")
        self.assertIn("Not yet available", planned.text)
        self.assertIn("not operational yet", planned.text)
        items = planned.find_all("li")
        self.assertEqual(len(items), 4)
        for name in ("Maize yield prediction", "Fertilizer recommendations", "Return on investment estimates", "USSD access for farmers"):
            matching = [item for item in items if name in item.text]
            self.assertEqual(len(matching), 1)
            self.assertIn("Planned", matching[0].text)
            self.assertNotIn(name, current.text)
        for available in ("Farmer account registration and login", "Farm-plot registration and management",
                          "Agricultural officer registration and login after approval", "Read-only access to assigned farmers and farm plots"):
            self.assertIn(available, current.text)
        self.assertIn("requires administrator approval before account access is enabled", page.root.text)

    def test_stylesheets_load_in_order_and_homepage_requires_no_scripts_or_external_assets(self):
        _, page = self.page()
        styles = [element.attrs["href"] for element in page.root.find_all("link") if element.attrs.get("rel") == "stylesheet"]
        self.assertEqual(styles, ["/static/css/registration.css", "/static/css/home.css"])
        for path in styles:
            with self.subTest(path=path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, "text/css")
                self.assertTrue(response.get_data())
        self.assertEqual(page.root.find_all("script"), [])
        self.assertEqual(page.root.find_all("form"), [])
        self.assertEqual(page.root.find_all("iframe"), [])
        for element in page.elements:
            for attr in ("href", "src"):
                if attr in element.attrs:
                    self.assertTrue(element.attrs[attr].startswith(("/", "#")))

    def test_query_values_and_config_secrets_are_never_reflected(self):
        response, _ = self.page(path="/?name=private-farmer-name&email=private%40example.test&password=private-password")
        html = response.get_data(as_text=True)
        for private in ("private-farmer-name", "private@example.test", "private-password", "private-database-password",
                        "private-database-user", "isolated-homepage-test-key"):
            self.assertNotIn(private, html)

    def test_farmer_and_officer_sessions_are_unchanged_and_homepage_is_account_independent(self):
        anonymous, _ = self.page()
        for identity in ({"user_id": 987654, "farmer_id": 876543},
                         {"user_id": 987654, "agricultural_officer_id": 765432}):
            with self.subTest(identity=identity):
                client = self.app.test_client()
                private_session = dict(identity, first_name="private-account-name", email="private@example.test",
                                       password="private-password", password_hash="private-hash", csrf_token="private-session-token")
                with client.session_transaction() as session:
                    session.update(private_session)
                response, _ = self.page(client)
                self.assertEqual(response.get_data(), anonymous.get_data())
                self.assertNotIn("Set-Cookie", response.headers)
                with client.session_transaction() as session:
                    self.assertEqual(dict(session), private_session)


if __name__ == "__main__":
    unittest.main()
