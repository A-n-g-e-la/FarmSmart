import base64
import json
import os
import runpy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import dotenv
from flask import Flask

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app.models.ussd_repository import ussd_transaction
    from app.services.ussd_pin_service import (
        HASH_METHOD, SALT_LENGTH, UssdPinConfigurationError, UssdPinError,
        UssdPinValidationError, hash_ussd_pin, verify_ussd_pin,
    )


# Fixed synthetic fixtures only; never production secrets or persistent keys.
PEPPER_ONE = base64.b64encode(b"1" * 32).decode("ascii")
PEPPER_TWO = base64.b64encode(b"2" * 32).decode("ascii")
METADATA_KEY = base64.b64encode(b"m" * 32).decode("ascii")


def test_app():
    app = Flask("ussd-pin-offline-tests")
    app.config.update(
        TESTING=True, SECRET_KEY="synthetic-web-cookie-key",
        USSD_PIN_PEPPERS=json.dumps({"1": PEPPER_ONE, "2": PEPPER_TWO}),
        USSD_PIN_CURRENT_KEY_VERSION="1", USSD_METADATA_HMAC_KEY=METADATA_KEY,
    )
    return app


class UssdPinServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            with test_app().app_context():
                cls.credential = hash_ussd_pin(91, "0042")
            guard.assert_not_called()

    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.connector = guard.start()
        self.addCleanup(guard.stop)
        self.app = test_app()
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)

    def tearDown(self):
        self.connector.assert_not_called()

    def verify(self, pin="0042", farmer_id=91, version=1, pin_hash=None):
        return verify_ussd_pin(farmer_id, pin,
                               self.credential["pin_hash"] if pin_hash is None else pin_hash, version)

    def test_correct_wrong_and_other_farmer_pins(self):
        self.assertTrue(self.verify())
        self.assertFalse(self.verify(pin="0043"))
        self.assertFalse(self.verify(farmer_id=92))

    def test_four_digit_boundaries_and_leading_zeros_are_preserved(self):
        for pin in ("0000", "9999"):
            with self.subTest(pin=pin):
                credential = hash_ussd_pin(91, pin)
                self.assertTrue(verify_ussd_pin(91, pin, credential["pin_hash"], 1))
        self.assertFalse(self.verify(pin="42"))
        self.assertFalse(self.verify(pin=42))

    def test_invalid_pin_formats_fail_without_hashing(self):
        values = (None, 1234, True, 12.34, b"0042", [], {}, "", "123", "12345",
                  " 0042", "0042 ", "0042\n", "00*2", "-042", "abcd", "１２３４", "٠٠٤٢", "1" * 10000)
        with patch("app.services.ussd_pin_service.generate_password_hash") as generate:
            with patch("app.services.ussd_pin_service.check_password_hash") as check:
                for pin in values:
                    with self.subTest(pin_type=type(pin).__name__):
                        with self.assertRaisesRegex(UssdPinValidationError, "^Use exactly four ASCII digits for the USSD PIN\\.$"):
                            hash_ussd_pin(91, pin)
                        self.assertFalse(self.verify(pin=pin))
                generate.assert_not_called()
                check.assert_not_called()

    def test_invalid_farmer_ids_are_safe(self):
        for farmer_id in (None, True, "91", 0, -1, 2**64):
            with self.subTest(farmer_id=farmer_id):
                with self.assertRaisesRegex(UssdPinValidationError, "^A valid farmer ID is required\\.$"):
                    hash_ussd_pin(farmer_id, "0042")
                self.assertFalse(self.verify(farmer_id=farmer_id))

    def test_secure_salt_work_factor_and_minimal_storage_result(self):
        second = hash_ussd_pin(91, "0042")
        self.assertEqual(set(second), {"pin_hash", "pin_key_version"})
        self.assertEqual(second["pin_key_version"], 1)
        method, salt, digest = second["pin_hash"].split("$")
        self.assertEqual(method, "scrypt:131072:8:1")
        self.assertEqual(len(salt), 32)
        self.assertEqual(len(digest), 128)
        self.assertLessEqual(len(second["pin_hash"]), 255)
        self.assertNotEqual(second["pin_hash"], self.credential["pin_hash"])
        self.assertNotEqual(salt, self.credential["pin_hash"].split("$")[1])
        self.assertTrue(verify_ussd_pin(91, "0042", second["pin_hash"], 1))

    def test_hash_library_receives_peppered_material_not_plaintext_pin(self):
        with patch("app.services.ussd_pin_service.generate_password_hash", return_value=self.credential["pin_hash"]) as generate:
            hash_ussd_pin(91, "0042")
            material = generate.call_args.args[0]
            self.assertRegex(material, r"^[0-9a-f]{64}$")
            self.assertNotEqual(material, "0042")
            self.assertNotEqual(material, PEPPER_ONE)
            self.assertEqual(generate.call_args.kwargs, {"method": HASH_METHOD, "salt_length": SALT_LENGTH})

    def test_invalid_or_unbounded_hash_envelopes_do_not_invoke_scrypt(self):
        invalid = (None, 1, b"hash", [], "", "0042", "pbkdf2:sha256:1$salt$hash",
                   self.credential["pin_hash"].replace("131072", "999999999"),
                   self.credential["pin_hash"].replace("131072", "32768"),
                   self.credential["pin_hash"] + "$extra", "a" * 10000)
        with patch("app.services.ussd_pin_service.check_password_hash") as check:
            for encoded in invalid:
                with self.subTest(encoded_type=type(encoded).__name__):
                    self.assertFalse(verify_ussd_pin(91, "0042", encoded, 1))
            check.assert_not_called()

    def test_missing_configuration_fails_closed_without_hashing(self):
        for field in ("USSD_PIN_PEPPERS", "USSD_PIN_CURRENT_KEY_VERSION"):
            original = self.app.config[field]
            for value in (None, ""):
                with self.subTest(field=field, value=value):
                    self.app.config[field] = value
                    with patch("app.services.ussd_pin_service.generate_password_hash") as generate:
                        with self.assertRaisesRegex(UssdPinConfigurationError, "^USSD PIN verification is unavailable\\.$"):
                            hash_ussd_pin(91, "0042")
                        with self.assertRaises(UssdPinConfigurationError):
                            self.verify()
                        generate.assert_not_called()
            self.app.config[field] = original

    def test_malformed_keyrings_and_encodings_have_fixed_errors(self):
        invalid = ("not json", "[]", "{}", "null", json.dumps({"1": "short"}),
                   json.dumps({"1": "!" * 44}), json.dumps({"1": "a" * 44}),
                   json.dumps({"0": PEPPER_ONE}), json.dumps({"01": PEPPER_ONE}),
                   json.dumps({"65536": PEPPER_ONE}), json.dumps({"1": None}),
                   json.dumps({"1": {"nested": "value"}}),
                   '{"1":"' + PEPPER_ONE + '","1":"' + PEPPER_TWO + '"}',
                   json.dumps({"1": PEPPER_ONE, "2": PEPPER_ONE}), "x" * 8193)
        for raw in invalid:
            with self.subTest(raw_type=type(raw).__name__):
                self.app.config["USSD_PIN_PEPPERS"] = raw
                with self.assertRaises(UssdPinConfigurationError) as error:
                    hash_ussd_pin(91, "0042")
                self.assertEqual(str(error.exception), "USSD PIN verification is unavailable.")

    def test_active_and_stored_version_validation(self):
        for value in (None, True, 0, -1, 65536, "01", "1.0", " 1", "3"):
            with self.subTest(active=value):
                self.app.config["USSD_PIN_CURRENT_KEY_VERSION"] = value
                with self.assertRaises(UssdPinConfigurationError):
                    hash_ussd_pin(91, "0042")
        self.app.config["USSD_PIN_CURRENT_KEY_VERSION"] = 1
        for value in (None, True, "1", 0, -1, 65536, []):
            with self.subTest(stored=value):
                self.assertFalse(self.verify(version=value))

    def test_rotation_uses_stored_version_and_keeps_old_credentials_valid(self):
        self.app.config["USSD_PIN_CURRENT_KEY_VERSION"] = "2"
        self.assertTrue(self.verify(version=1))
        self.assertFalse(self.verify(version=2))
        second = hash_ussd_pin(91, "0042")
        self.assertEqual(second["pin_key_version"], 2)
        self.assertTrue(verify_ussd_pin(91, "0042", second["pin_hash"], 2))
        self.assertFalse(verify_ussd_pin(91, "0042", second["pin_hash"], 1))
        self.app.config["USSD_PIN_PEPPERS"] = json.dumps({"2": PEPPER_TWO})
        with self.assertRaises(UssdPinConfigurationError):
            self.verify(version=1)
        self.assertTrue(verify_ussd_pin(91, "0042", second["pin_hash"], 2))

    def test_unknown_stored_version_is_operational_not_wrong_pin(self):
        with self.assertRaisesRegex(UssdPinConfigurationError, "^USSD PIN verification is unavailable\\.$"):
            self.verify(version=3)

    def test_secret_reuse_is_rejected_in_raw_and_encoded_forms(self):
        for field in ("SECRET_KEY", "USSD_METADATA_HMAC_KEY"):
            original = self.app.config[field]
            for reused in (b"1" * 32, "1" * 32, PEPPER_ONE):
                with self.subTest(field=field, representation=type(reused).__name__):
                    self.app.config[field] = reused
                    with self.assertRaises(UssdPinConfigurationError):
                        hash_ussd_pin(91, "0042")
            self.app.config[field] = original

    def test_absent_metadata_key_does_not_generate_or_reuse_a_key(self):
        self.app.config["USSD_METADATA_HMAC_KEY"] = None
        self.assertTrue(self.verify())
        self.assertIsNone(self.app.config["USSD_METADATA_HMAC_KEY"])

    def test_crypto_errors_are_sanitized_without_logging(self):
        for function in ("generate_password_hash", "check_password_hash"):
            for kind in (ValueError, RuntimeError):
                with self.subTest(function=function, kind=kind):
                    with patch("app.services.ussd_pin_service." + function,
                               side_effect=kind("synthetic-sensitive-diagnostic")):
                        with self.assertNoLogs(level="DEBUG"):
                            with self.assertRaises(UssdPinError) as error:
                                if function == "generate_password_hash":
                                    hash_ussd_pin(91, "0042")
                                else:
                                    self.verify()
                        self.assertEqual(str(error.exception), "USSD PIN operation could not be completed.")
                        self.assertTrue(error.exception.__suppress_context__)

    def test_no_logging_on_success_or_mismatch(self):
        with self.assertNoLogs(level="DEBUG"):
            self.assertTrue(self.verify())
            self.assertFalse(self.verify(pin="0043"))

    def test_real_hash_is_accepted_by_stage_2b_repository_without_database_access(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        cursor.rowcount = 1
        cursor.fetchone.return_value = {"farmer_id": 91, "pin_version": 1}
        with patch("app.models.ussd_repository.get_db_connection", return_value=connection):
            with ussd_transaction() as store:
                store.replace_pin_hash(91, self.credential["pin_hash"],
                                       self.credential["pin_key_version"], datetime.now(timezone.utc))
        connection.commit.assert_called_once_with()

    def test_config_reads_environment_without_generating_secrets(self):
        path = Path(__file__).resolve().parents[1] / "app" / "config" / "__init__.py"
        values = {"USSD_PIN_PEPPERS": json.dumps({"1": PEPPER_ONE}),
                  "USSD_PIN_CURRENT_KEY_VERSION": "1", "USSD_METADATA_HMAC_KEY": METADATA_KEY}
        with patch.dict(os.environ, values, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
            config = runpy.run_path(str(path))["Config"]
        for name, value in values.items():
            self.assertEqual(getattr(config, name), value)
        with patch.dict(os.environ, {}, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
            missing = runpy.run_path(str(path))["Config"]
        self.assertIsNone(missing.USSD_PIN_PEPPERS)
        self.assertIsNone(missing.USSD_PIN_CURRENT_KEY_VERSION)


if __name__ == "__main__":
    unittest.main()
