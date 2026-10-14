import unittest
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error
from werkzeug.security import generate_password_hash

# Never load local credentials; the real connector is guarded in every test.
with patch.object(dotenv, "load_dotenv", return_value=False):
    from app.services.authentication_service import authenticate_farmer


class FarmerAuthenticationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "synthetic-login-password"
        cls.password_hash = generate_password_hash(cls.password, method="scrypt")

    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.user = {
            "user_id": 41, "farmer_id": 91, "role": "farmer",
            "status": "active", "password_hash": self.password_hash,
        }
        self.cursor.fetchone.return_value = self.user
        helper = patch(
            "app.services.authentication_service.get_db_connection",
            return_value=self.connection,
        )
        self.mock_helper = helper.start()
        self.addCleanup(helper.stop)
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_guard = guard.start()
        self.addCleanup(guard.stop)

    def assert_invalid(self, result):
        self.assertEqual(result, {
            "ok": False, "code": "invalid_credentials",
            "message": "Invalid phone number or password.",
        })

    def test_success_and_minimal_identity(self):
        result = authenticate_farmer("0712345678", self.password)
        self.assertEqual(result, {"ok": True, "identity": {"user_id": 41, "farmer_id": 91}})
        sql, parameters = self.cursor.execute.call_args.args
        self.assertIn("WHERE u.phone_number = %s", sql)
        self.assertIn("f.user_id = u.user_id", sql)
        self.assertEqual(parameters, ("+254712345678",))
        self.assertNotIn("+254712345678", sql)
        self.assertNotIn(self.password, str(result))
        self.assertNotIn(self.password_hash, str(result))
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        self.live_guard.assert_not_called()

    def test_incorrect_password(self):
        self.assert_invalid(authenticate_farmer("0712345678", "incorrect"))

    def test_nonexistent_user(self):
        self.cursor.fetchone.return_value = None
        self.assert_invalid(authenticate_farmer("0712345678", self.password))

    def test_unknown_user_still_verifies_a_hash(self):
        self.cursor.fetchone.return_value = None
        with patch("app.services.authentication_service.check_password_hash", return_value=True) as verify:
            self.assert_invalid(authenticate_farmer("0712345678", self.password))
        verify.assert_called_once()
        self.assertTrue(verify.call_args.args[0].startswith("scrypt:"))

    def test_inactive_accounts(self):
        for status in ("pending", "disabled"):
            with self.subTest(status=status):
                self.user["status"] = status
                self.assert_invalid(authenticate_farmer("0712345678", self.password))

    def test_incorrect_role(self):
        self.user["role"] = "officer"
        self.assert_invalid(authenticate_farmer("0712345678", self.password))

    def test_missing_farmer_profile(self):
        self.user["farmer_id"] = None
        self.assert_invalid(authenticate_farmer("0712345678", self.password))

    def test_phone_normalization(self):
        for number in ("0712 345 678", "+254712345678", "254712345678", "00254712345678", "712345678"):
            with self.subTest(number=number):
                self.assertTrue(authenticate_farmer(number, self.password)["ok"])
                self.assertEqual(self.cursor.execute.call_args.args[1], ("+254712345678",))
        self.assertTrue(authenticate_farmer("0112345678", self.password)["ok"])
        self.assertEqual(self.cursor.execute.call_args.args[1], ("+254112345678",))

    def test_invalid_phone(self):
        for value in (None, "invalid", "+255712345678", "0201234567", 712345678):
            self.assert_invalid(authenticate_farmer(value, self.password))
        self.mock_helper.assert_not_called()

    def test_invalid_password_input(self):
        for value in (None, "", 123, "x" * 1025):
            self.assert_invalid(authenticate_farmer("0712345678", value))
        self.mock_helper.assert_not_called()

    def test_password_whitespace_is_not_trimmed(self):
        self.user["password_hash"] = generate_password_hash("  synthetic-password  ", method="scrypt")
        self.assertTrue(authenticate_farmer("0712345678", "  synthetic-password  ")["ok"])
        self.assert_invalid(authenticate_farmer("0712345678", "synthetic-password"))

    def test_connection_error_has_safe_server_result(self):
        self.mock_helper.side_effect = Error("private-credentials")
        with self.assertLogs("app.services.authentication_service", level="ERROR") as logs:
            result = authenticate_farmer("0712345678", self.password)
        self.assertEqual(result["code"], "authentication_error")
        self.assertNotIn("private-credentials", str(result) + str(logs.output))
        self.assertNotIn(self.password, str(result) + str(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_query_error_releases_resources(self):
        self.cursor.execute.side_effect = Error("private-detail")
        with self.assertLogs("app.services.authentication_service", level="ERROR"):
            result = authenticate_farmer("0712345678", self.password)
        self.assertEqual(result["code"], "authentication_error")
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_unexpected_verification_error_is_safe(self):
        with patch("app.services.authentication_service.check_password_hash", side_effect=ValueError("private-hash")), self.assertLogs("app.services.authentication_service", level="ERROR") as logs:
            result = authenticate_farmer("0712345678", self.password)
        self.assertEqual(result["code"], "authentication_error")
        self.assertNotIn("private-hash", str(result) + str(logs.output))
        self.connection.close.assert_called_once_with()

    def test_cursor_close_failure_still_closes_connection(self):
        self.cursor.close.side_effect = Error("private-detail")
        with self.assertLogs("app.services.authentication_service", level="WARNING") as logs:
            result = authenticate_farmer("0712345678", self.password)
        self.assertTrue(result["ok"])
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private-detail", str(logs.output))


if __name__ == "__main__":
    unittest.main()
