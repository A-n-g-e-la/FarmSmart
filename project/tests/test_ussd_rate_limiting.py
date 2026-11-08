import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import os
import runpy
import unittest
from unittest.mock import patch
from pathlib import Path

import dotenv

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.ussd_callback_security import CallbackLimit
    from app.services.ussd_rate_limiter import LocalUssdLimiter


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def quota(scope="phone", key=b"synthetic", count=20, seconds=60, burst=5):
    return CallbackLimit(scope, key, count, seconds, burst)


class LimiterAlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.limiter = LocalUssdLimiter(clock=self.clock)

    def test_burst_and_fractional_refill_boundary(self):
        limits = (quota(),)
        for _ in range(5):
            self.assertTrue(self.limiter.admit(limits))
        self.assertFalse(self.limiter.admit(limits))
        self.clock.advance(2.99)
        self.assertFalse(self.limiter.admit(limits))
        self.clock.advance(0.01)
        self.assertTrue(self.limiter.admit(limits))

    def test_sliding_window_rejects_even_with_refilled_tokens(self):
        limits = (quota(count=4, seconds=4, burst=2),)
        self.assertTrue(self.limiter.admit(limits))
        self.assertTrue(self.limiter.admit(limits))
        for _ in range(2):
            self.clock.advance(1)
            self.assertTrue(self.limiter.admit(limits))
        self.clock.advance(1)
        self.assertFalse(self.limiter.admit(limits))
        self.clock.advance(1)
        self.assertTrue(self.limiter.admit(limits))

    def test_combined_denial_consumes_neither_quota(self):
        initial = quota("initial", count=1, burst=1)
        phone = quota(count=5, burst=5)
        self.assertTrue(self.limiter.admit((initial,)))
        self.assertFalse(self.limiter.admit((phone, initial)))
        self.assertNotIn((phone.scope, phone.key), self.limiter._entries)
        for _ in range(5):
            self.assertTrue(self.limiter.admit((phone,)))
        self.assertFalse(self.limiter.admit((phone,)))

    def test_capacity_refuses_without_evicting_active_entries_or_partial_insert(self):
        original = quota(key=b"first", count=1, burst=1)
        self.assertTrue(self.limiter.admit((original,), max_keys=2))
        with self.assertRaises(ValueError):
            self.limiter.admit((quota(key=b"second"), quota("initial", b"second")), max_keys=2)
        self.assertEqual(len(self.limiter._entries), 1)
        self.assertFalse(self.limiter.admit((original,), max_keys=2))
        self.clock.advance(60)
        self.assertTrue(self.limiter.admit((quota(key=b"second"), quota("initial", b"second")), max_keys=2))
        self.assertEqual(len(self.limiter._entries), 2)

    def test_expired_keys_are_reclaimed_under_many_identities(self):
        for index in range(100):
            self.assertTrue(self.limiter.admit((quota(key=str(index).encode()),), max_keys=1))
            self.assertEqual(len(self.limiter._entries), 1)
            self.clock.advance(60)

    def test_concurrent_combined_admission_never_exceeds_quota(self):
        limits = (quota(count=20, burst=20), quota("initial", count=3, burst=3))
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.limiter.admit(limits), range(100)))
        self.assertEqual(sum(results), 3)
        self.assertEqual(len(self.limiter._entries[("phone", b"synthetic")].accepted), 3)

    def test_bad_clock_fails_and_lock_is_released(self):
        self.assertTrue(self.limiter.admit((quota(),)))
        for bad_time in (99, float("nan"), float("inf")):
            self.clock.now = bad_time
            with self.assertRaises(ValueError):
                self.limiter.admit((quota(),))
        self.clock.now = 101
        self.assertTrue(self.limiter.admit((quota(),)))

    def test_changed_policy_and_invalid_limits_are_refused(self):
        self.assertTrue(self.limiter.admit((quota(),)))
        invalid = ((quota(count=19),), (quota(burst=True),), (quota(), quota()), ())
        for limits in invalid:
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.limiter.admit(limits)


class UssdRateLimitingRouteTests(unittest.TestCase):
    def setUp(self):
        self.db = patch("mysql.connector.connect", side_effect=AssertionError("Database forbidden")).start()
        self.addCleanup(patch.stopall)
        self.verify = patch("app.services.ussd_authentication_service.verify_ussd_pin").start()
        self.hash_pin = patch("app.services.ussd_pin_service.generate_password_hash").start()
        self.check_pin = patch("app.services.ussd_pin_service.check_password_hash").start()
        self.app = create_app()
        self.app.config.update(
            TESTING=True, USSD_PERSISTENT_FLOW_ENABLED=False, USSD_RATE_LIMITING_ENABLED=True,
            USSD_GLOBAL_CALLBACKS_PER_SECOND=10, USSD_GLOBAL_CALLBACK_BURST=10,
            USSD_PHONE_CALLBACKS_PER_MINUTE=20, USSD_PHONE_CALLBACK_BURST=5,
            USSD_NEW_SESSIONS_PER_MINUTE=3, USSD_RATE_LIMIT_MAX_KEYS=4096,
        )
        self.clock = FakeClock()
        self.limiter = LocalUssdLimiter(clock=self.clock)
        self.app.extensions["ussd_local_limiter"] = self.limiter
        self.client = self.app.test_client()
        self.payload = {"sessionId": "synthetic-session", "serviceCode": "*384*123#",
                        "phoneNumber": "+254700000000", "text": ""}

    def tearDown(self):
        for dependency in (self.db, self.verify, self.hash_pin, self.check_pin):
            dependency.assert_not_called()

    def post(self, **changes):
        return self.client.post("/ussd", data=dict(self.payload, **changes))

    def assert_refused(self, response, status):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response.content_type, "text/plain; charset=utf-8")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.get_data(as_text=True),
                         "END Verification could not be completed. Please try again later.")

    def test_normal_legacy_navigation_and_optional_field(self):
        expected = {
            "": "CON Welcome to FarmAdvis\n1. Request maize advice\n0. Exit",
            "0": "END Thank you for using FarmAdvis.",
            "1": "END Account verification is not yet available. Please try again later.",
            "2": "END Invalid selection. Please dial again.",
        }
        for text, message in expected.items():
            response = self.post(text=text, networkCode="synthetic")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(as_text=True), message)

    def test_global_limit_counts_different_phones_and_sessions(self):
        for index in range(10):
            self.assertEqual(self.post(phoneNumber=f"+2547000000{index:02d}", sessionId=str(index)).status_code, 200)
        self.assert_refused(self.post(phoneNumber="+254700000099"), 429)
        self.clock.advance(1)
        self.assertEqual(self.post(phoneNumber="+254700000099").status_code, 200)

    def test_global_denial_precedes_body_reading(self):
        self.app.config.update(USSD_GLOBAL_CALLBACKS_PER_SECOND=1, USSD_GLOBAL_CALLBACK_BURST=1)
        self.assertEqual(self.post(text="0").status_code, 200)
        class UnreadableBody(BytesIO):
            def read(self, *args):
                raise AssertionError("Body must not be read")
            def readinto(self, *args):
                raise AssertionError("Body must not be read")
        response = self.client.open("/ussd", method="POST", environ_overrides={
            "wsgi.input": UnreadableBody(b"sensitive"), "CONTENT_LENGTH": "9",
            "CONTENT_TYPE": "application/x-www-form-urlencoded"})
        self.assert_refused(response, 429)

    def test_phone_burst_cannot_be_reset_by_session_or_format(self):
        for index in range(5):
            self.assertEqual(self.post(text="0", sessionId=str(index)).status_code, 200)
        self.assert_refused(self.post(text="0", phoneNumber="0700000000", sessionId="different"), 429)
        self.clock.advance(3)
        self.assertEqual(self.post(text="0").status_code, 200)

    def test_phone_sliding_minute_limit(self):
        for index in range(20):
            self.assertEqual(self.post(text="0").status_code, 200)
            self.clock.advance(2.9 if index < 4 else 3)
        self.assert_refused(self.post(text="0"), 429)
        self.clock.now = 160
        self.assertEqual(self.post(text="0").status_code, 200)

    def test_initial_quota_counts_retries_and_resets_after_window(self):
        for index in range(3):
            self.assertEqual(self.post(sessionId=str(index)).status_code, 200)
        self.assert_refused(self.post(sessionId="new-id"), 429)
        self.assertEqual(self.post(text="0").status_code, 200)
        self.clock.advance(60)
        self.assertEqual(self.post().status_code, 200)

    def test_duplicate_callbacks_consume_quota_without_session_state(self):
        for _ in range(5):
            self.assertEqual(self.post(text="1").status_code, 200)
        self.assert_refused(self.post(text="1"), 429)

    def test_phone_state_contains_only_keyed_digests(self):
        self.post()
        identities = list(self.limiter._entries)
        self.assertEqual(len(identities), 3)
        tags = [key for scope, key in identities if scope != "global"]
        self.assertTrue(all(isinstance(tag, bytes) and len(tag) == 32 for tag in tags))
        self.assertEqual(tags[0], tags[1])
        self.assertNotIn(self.payload["phoneNumber"], repr(self.limiter.__dict__))
        self.assertNotIn(self.payload["sessionId"], repr(self.limiter.__dict__))

    def test_enabled_failure_missing_adapter_and_bad_result_are_safe(self):
        for result in (1, None):
            with patch.object(self.limiter, "admit", return_value=result):
                self.assert_refused(self.post(), 503)
        with patch.object(self.limiter, "admit", side_effect=RuntimeError("synthetic-private-detail")):
            self.assert_refused(self.post(), 503)
        self.app.extensions.pop("ussd_local_limiter")
        self.assert_refused(self.post(), 503)

    def test_invalid_flag_and_limits_fail_closed_before_body(self):
        for setting, value in (("USSD_RATE_LIMITING_ENABLED", "true"),
                               ("USSD_RATE_LIMIT_MAX_KEYS", 0),
                               ("USSD_PHONE_CALLBACK_BURST", 21),
                               ("USSD_GLOBAL_CALLBACKS_PER_SECOND", True)):
            original = self.app.config[setting]
            self.app.config[setting] = value
            self.assert_refused(self.post(), 503)
            self.app.config[setting] = original

    def test_invalid_capacity_is_rejected_before_any_admission(self):
        for capacity in (1, 2, "1", "2", 0, -1, 20001, "20001", True, 3.0):
            with self.subTest(capacity=capacity):
                self.app.config["USSD_RATE_LIMIT_MAX_KEYS"] = capacity
                with patch.object(self.limiter, "admit", wraps=self.limiter.admit) as admission:
                    self.assert_refused(self.post(), 503)
                    admission.assert_not_called()
                self.assertEqual(self.limiter._entries, {})
                self.assertIsNone(self.limiter._last_time)
                for dependency in (self.db, self.verify, self.hash_pin, self.check_pin):
                    dependency.assert_not_called()

    def test_supported_capacity_boundaries_admit_welcome(self):
        for capacity in (3, "3", 20000, "20000"):
            with self.subTest(capacity=capacity):
                self.app.config["USSD_RATE_LIMIT_MAX_KEYS"] = capacity
                self.limiter = LocalUssdLimiter(clock=self.clock)
                self.app.extensions["ussd_local_limiter"] = self.limiter
                self.assertEqual(self.post().status_code, 200)
                self.assertEqual(len(self.limiter._entries), 3)

    def test_capacity_exhaustion_is_safe_and_does_not_evict_active_phone(self):
        self.app.config["USSD_RATE_LIMIT_MAX_KEYS"] = 3
        self.assertEqual(self.post().status_code, 200)
        self.assert_refused(self.post(phoneNumber="+254700000001"), 503)
        self.assertEqual(len(self.limiter._entries), 3)
        self.assertEqual(self.post(text="0").status_code, 200)
        self.clock.advance(60)
        self.assertEqual(self.post(phoneNumber="+254700000001").status_code, 200)

    def test_concurrent_route_requests_are_limited(self):
        def submit(_):
            with self.app.test_client() as client:
                return client.post("/ussd", data=dict(self.payload, text="0")).status_code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(40)))
        self.assertEqual(results.count(200), 5)
        self.assertEqual(results.count(429), 35)

    def test_disabled_limiter_preserves_legacy_and_does_not_consult_adapter(self):
        self.app.config["USSD_RATE_LIMITING_ENABLED"] = False
        with patch.object(self.limiter, "admit", side_effect=AssertionError("Disabled")):
            for _ in range(25):
                self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post(unexpected="synthetic").status_code, 400)
            self.assertEqual(self.client.post("/ussd", json={}).status_code, 415)

    def test_existing_validation_still_precedes_phone_admission(self):
        for changes in ({"phoneNumber": "invalid"}, {"serviceCode": "bad"}, {"text": "x"}, {"unexpected": "x"}):
            self.assertEqual(self.post(**changes).status_code, 400)
        self.assertEqual(list(self.limiter._entries), [("global", b"ussd-global")])

    def test_local_limiter_does_not_satisfy_persistent_security(self):
        self.assertFalse(self.limiter.deployment_wide)
        self.app.config.update(
            USSD_PERSISTENT_FLOW_ENABLED=True, SECRET_KEY="synthetic-flask",
            USSD_SERVICE_CODE="*384*123#", USSD_TRUSTED_RELAY_IPS="127.0.0.1",
            USSD_RELAY_SECRET=base64.b64encode(b"r" * 32).decode(),
            USSD_METADATA_HMAC_KEY=base64.b64encode(b"m" * 32).decode(),
            USSD_PIN_PEPPERS='{"1":"' + base64.b64encode(b"p" * 32).decode() + '"}',
            USSD_PIN_CURRENT_KEY_VERSION="1",
        )
        self.app.extensions["ussd_shared_limiter"] = self.limiter
        self.assert_refused(self.post(), 503)

    def test_rate_limiting_defaults_off_and_malformed_env_does_not_enable_it(self):
        config_path = Path(__file__).resolve().parents[1] / "app" / "config" / "__init__.py"
        for value, expected in ((None, False), ("false", False), ("true", True), ("yes", "invalid")):
            env = {} if value is None else {"USSD_RATE_LIMITING_ENABLED": value}
            with patch.dict(os.environ, env, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
                config = runpy.run_path(str(config_path))["Config"]
            self.assertEqual(config.USSD_RATE_LIMITING_ENABLED, expected)
            self.assertIs(config.USSD_PERSISTENT_FLOW_ENABLED, False)


if __name__ == "__main__":
    unittest.main()
