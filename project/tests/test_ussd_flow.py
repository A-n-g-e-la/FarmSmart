import unittest
from io import BytesIO
from unittest.mock import patch

import dotenv
from werkzeug.datastructures import MultiDict

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.routes.ussd import MAX_CALLBACK_BYTES


class UssdCallbackTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.connector = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-ussd-test-key", USSD_PERSISTENT_FLOW_ENABLED=False)
        self.client = self.app.test_client()
        self.payload = {
            "sessionId": "ATUid_test-session.1:2",
            "serviceCode": "*384*123#",
            "phoneNumber": "+254712345678",
            "text": "",
        }

    def tearDown(self):
        self.connector.assert_not_called()

    def post(self, **changes):
        return self.client.post("/ussd", data=dict(self.payload, **changes))

    def assert_reply(self, response, status, prefix):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.get_data(as_text=True).startswith(prefix))
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertNotIn(self.payload["phoneNumber"], response.get_data(as_text=True))
        self.assertNotIn(self.payload["sessionId"], response.get_data(as_text=True))

    def test_welcome_is_public_and_registered_once(self):
        response = self.post()
        self.assert_reply(response, 200, "CON ")
        self.assertEqual(response.get_data(as_text=True),
                         "CON Welcome to FarmAdvis\n1. Request maize advice\n0. Exit")
        rules = [rule for rule in self.app.url_map.iter_rules() if rule.rule == "/ussd"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].endpoint, "ussd.callback")

    def test_optional_network_code_preserves_initial_empty_text_welcome(self):
        for extra in ({}, {"networkCode": "99999"}, {"networkCode": "untrusted-metadata"}):
            with self.subTest(extra=extra):
                response = self.post(**extra)
                self.assert_reply(response, 200, "CON ")
                self.assertEqual(response.get_data(as_text=True),
                                 "CON Welcome to FarmAdvis\n1. Request maize advice\n0. Exit")

    def test_network_code_does_not_relax_required_or_duplicate_field_checks(self):
        payload = dict(self.payload, networkCode="99999")
        for key in payload:
            duplicate = MultiDict(payload)
            duplicate.add(key, payload[key])
            with self.subTest(duplicate=key):
                self.assert_reply(self.client.post("/ussd", data=duplicate), 400, "END ")
            if key != "networkCode":
                missing = dict(payload)
                del missing[key]
                with self.subTest(missing=key):
                    self.assert_reply(self.client.post("/ussd", data=missing), 400, "END ")
        self.assert_reply(self.post(networkCode="99999", unexpected="synthetic"), 400, "END ")

    def test_advice_option_ends_without_accepting_a_request(self):
        response = self.post(text="1")
        self.assert_reply(response, 200, "END ")
        self.assertIn("Account verification is not yet available", response.get_data(as_text=True))

    def test_exit(self):
        response = self.post(text="0")
        self.assert_reply(response, 200, "END Thank you")

    def test_invalid_choices_and_cumulative_history_terminate(self):
        for text in ("2", "00", "01", "1*1", "1*0", "9*1*0"):
            with self.subTest(text=text):
                self.assert_reply(self.post(text=text), 200, "END Invalid selection.")

    def test_missing_empty_duplicate_and_unexpected_fields(self):
        for key in self.payload:
            with self.subTest(missing=key):
                data = dict(self.payload)
                del data[key]
                self.assert_reply(self.client.post("/ussd", data=data), 400, "END ")
            with self.subTest(duplicate=key):
                data = MultiDict(self.payload)
                data.add(key, self.payload[key])
                self.assert_reply(self.client.post("/ussd", data=data), 400, "END ")
            if key != "text":
                with self.subTest(empty=key):
                    self.assert_reply(self.post(**{key: ""}), 400, "END ")
        self.assert_reply(self.post(farmer_id="91"), 400, "END ")

    def test_invalid_formats_are_not_reflected(self):
        invalid = {
            "sessionId": [" ", "a" * 129, "bad\nsession", "<script>", "é"],
            "serviceCode": ["384", "*384", "*#", "*1234567#", "*1" * 20 + "#"],
            "phoneNumber": ["123", "+254712345678 ext 1", "a" * 41],
            "text": [" ", "advice", "*1", "1*", "1**0", "１", "1\n", "1" * 129, "*".join(["1"] * 17)],
        }
        for key, values in invalid.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    response = self.post(**{key: value})
                    self.assert_reply(response, 400, "END ")
                    self.assertEqual(response.get_data(as_text=True), "END Invalid request. Please dial again.")

    def test_phone_formats_and_shared_service_codes(self):
        for phone in ("0712 345 678", "254712345678", "+254112345678"):
            with self.subTest(phone=phone):
                self.assert_reply(self.post(phoneNumber=phone, serviceCode="*123#"), 200, "CON ")

    def test_unsupported_payload_types(self):
        for response in (
            self.client.post("/ussd", json=self.payload),
            self.client.post("/ussd", data="private-input", content_type="text/plain"),
            self.client.post("/ussd", data=self.payload, content_type="multipart/form-data"),
            self.client.post("/ussd"),
        ):
            self.assert_reply(response, 415, "END ")

    def test_oversized_requests_end_safely(self):
        self.assert_reply(self.post(text="1" * MAX_CALLBACK_BYTES), 413, "END Request too large.")

    def test_oversized_stream_without_declared_length(self):
        response = self.client.post("/ussd", environ_overrides={
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": "",
            "wsgi.input": BytesIO(b"text=" + b"1" * MAX_CALLBACK_BYTES),
            "wsgi.input_terminated": True,
        })
        self.assert_reply(response, 413, "END Request too large.")

    def test_truncated_body_has_safe_plain_text_error(self):
        response = self.client.post("/ussd", environ_overrides={
            "CONTENT_TYPE": "application/x-www-form-urlencoded",
            "CONTENT_LENGTH": "100",
            "wsgi.input": BytesIO(b"text="),
        })
        self.assert_reply(response, 400, "END Invalid request.")

    def test_callback_does_not_log_input(self):
        with self.assertNoLogs(level="DEBUG"):
            self.post()
            self.post(text="1")
            self.post(text="private-credential")
            self.post(text="1" * MAX_CALLBACK_BYTES)

    def test_query_parameters_cannot_supply_required_fields(self):
        self.assert_reply(self.client.post("/ussd", query_string=self.payload,
                                          data={}, content_type="application/x-www-form-urlencoded"), 400, "END ")

    def test_callback_does_not_read_or_change_browser_identity(self):
        identity = {"user_id": 41, "farmer_id": 91, "authenticated_at": 1}
        with self.client.session_transaction() as session:
            session.update(identity)
        self.assert_reply(self.post(text="1"), 200, "END ")
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), identity)
        # Subsequent callbacks also work without a shared cookie jar.
        self.assert_reply(self.app.test_client().post("/ussd", data=dict(self.payload, text="0")),
                          200, "END Thank you")

    def test_only_post_is_supported(self):
        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)("/ussd").status_code, 405)


if __name__ == "__main__":
    unittest.main()
