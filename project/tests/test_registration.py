import unittest
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error, IntegrityError
from werkzeug.security import check_password_hash

# Import the app without reading local credentials; all database calls are mocked.
with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.registration_service import normalize_kenyan_phone, register_farmer


class FarmerRegistrationTests(unittest.TestCase):
    def setUp(self):
        # Synthetic inputs and independent IDs expose accidental user/profile mixing.
        self.data = {
            "first_name": " Test ", "last_name": " Farmer ",
            "phone_number": "0712 345 678", "password": "test-password-123",
            "password_confirmation": "test-password-123", "location": " Eldoret ",
        }
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = None

        def execute(sql, params):
            self.cursor.lastrowid = 91 if sql.startswith("INSERT INTO farmers") else 41

        self.cursor.execute.side_effect = execute
        self.helper = patch(
            "app.services.registration_service.get_db_connection",
            return_value=self.connection,
        )
        self.mock_helper = self.helper.start()
        self.addCleanup(self.helper.stop)

    def assert_validation(self, field, changes):
        # Invalid input must never open a database connection.
        self.data.update(changes)
        result = register_farmer(self.data)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "validation_error")
        self.assertIn(field, result["errors"])
        self.mock_helper.assert_not_called()

    def test_success_linkage_and_password_hash(self):
        self.data.update(role="officer", status="pending", assigned_officer_id=999)
        result = register_farmer(self.data)
        self.assertEqual(result["user_id"], 41)
        self.assertEqual(result["farmer_id"], 91)
        self.assertTrue(result["ok"])
        calls = self.cursor.execute.call_args_list
        user_sql, user_params = calls[1].args
        self.assertIn("'farmer', 'active'", user_sql)
        self.assertEqual(user_params[:3], ("Test", "Farmer", "+254712345678"))
        self.assertNotEqual(user_params[3], self.data["password"])
        self.assertTrue(user_params[3].startswith("scrypt:"))
        self.assertTrue(check_password_hash(user_params[3], self.data["password"]))
        self.assertEqual(calls[2].args, (
            "INSERT INTO farmers (user_id, location) VALUES (%s, %s)", (41, "Eldoret")
        ))
        self.assertNotIn(self.data["password"], str(result))
        self.assertNotIn(user_params[3], str(result))
        self.connection.start_transaction.assert_called_once_with()
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_phone_normalization(self):
        for number in ("0712345678", "712345678", "+254712345678", "254712345678", "00254712345678", "+254 (712) 345-678"):
            with self.subTest(number=number):
                self.assertEqual(normalize_kenyan_phone(number), "+254712345678")
        self.assertEqual(normalize_kenyan_phone("0112345678"), "+254112345678")

    def test_invalid_phone(self):
        for number in (None, 712345678, "", "07123", "+255712345678", "+2540712345678", "0201234567", "0712345678ext1", "++254712345678", "０７１２３４５６７８"):
            with self.subTest(number=number):
                self.assertIsNone(normalize_kenyan_phone(number))
        self.assert_validation("phone_number", {"phone_number": "invalid"})

    def test_duplicate_phone(self):
        self.cursor.fetchone.return_value = (41,)
        result = register_farmer(self.data)
        self.assertEqual(result["code"], "duplicate_phone")
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()

    def test_duplicate_race(self):
        self.cursor.execute.side_effect = [None, IntegrityError(
            msg="Duplicate entry 'private detail' for key 'uq_users_phone_number'",
            errno=1062,
        )]
        result = register_farmer(self.data)
        self.assertEqual(result["code"], "duplicate_phone")
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.assertNotIn("private detail", str(result))

    def assert_duplicate_constraint_result(self, message, expected, errno=1062):
        # Simulate a racing INSERT after an empty precheck, without a live database.
        self.connection.reset_mock()
        self.cursor.execute.side_effect = [None, IntegrityError(msg=message, errno=errno)]
        log_context = (
            self.assertLogs("app.services.registration_service", level="ERROR")
            if expected == "database_error"
            else self.assertNoLogs("app.services.registration_service", level="WARNING")
        )
        with log_context as logs:
            result = register_farmer(self.data)
        self.assertEqual(result["code"], expected)
        self.connection.rollback.assert_called_once_with()
        self.connection.commit.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        output = str(result) + (str(logs.output) if logs is not None else "")
        for sensitive in ("private detail", "uq_users_phone_number", "uq_users_email", "PRIMARY"):
            self.assertNotIn(sensitive, output)

    def test_phone_constraint_duplicate(self):
        for key in ("'uq_users_phone_number'", "`uq_users_phone_number`", "'users.uq_users_phone_number'"):
            with self.subTest(key=key):
                self.assert_duplicate_constraint_result(
                    "Duplicate entry 'private detail' for key " + key, "duplicate_phone"
                )

    def test_primary_key_duplicate_is_database_error(self):
        self.assert_duplicate_constraint_result(
            "Duplicate entry 'private detail' for key 'PRIMARY'", "database_error"
        )

    def test_other_unique_constraint_duplicate_is_database_error(self):
        for key in ("uq_users_email", "uq_users_phone_number_other", "other_table.uq_users_phone_number"):
            with self.subTest(key=key):
                self.assert_duplicate_constraint_result(
                    "Duplicate entry 'private detail' for key '" + key + "'", "database_error"
                )

    def test_unidentified_duplicate_is_database_error(self):
        for message in (
            "", "private detail", "Duplicate entry for key 2",
            "Duplicate entry 'uq_users_phone_number' for key 'PRIMARY'",
            "Duplicate entry 'private detail' for key uq_users_phone_number",
            "Duplicate entry 'private detail' for key 'uq_users_phone_number' extra detail",
            "Duplicate entry 'private detail' for key 'uq_users_phone_number`",
        ):
            with self.subTest(message=message):
                self.assert_duplicate_constraint_result(message, "database_error")

    def test_phone_constraint_without_duplicate_errno_is_database_error(self):
        self.assert_duplicate_constraint_result(
            "Failure for key 'uq_users_phone_number'", "database_error", errno=1452
        )

    def test_password_mismatch(self):
        self.assert_validation("password_confirmation", {"password_confirmation": "different"})

    def test_password_too_short(self):
        self.assert_validation("password", {"password": "short", "password_confirmation": "short"})

    def test_required_fields(self):
        for field in ("first_name", "last_name", "phone_number", "password", "password_confirmation"):
            with self.subTest(field=field):
                data = dict(self.data)
                del data[field]
                result = register_farmer(data)
                self.assertIn(field, result["errors"])
        self.mock_helper.assert_not_called()

    def test_field_limits_and_types(self):
        for field, value in (("first_name", "x" * 101), ("last_name", 123), ("location", "x" * 151), ("location", []), ("password", "x" * 1025)):
            with self.subTest(field=field):
                data = dict(self.data, **{field: value})
                self.assertIn(field, register_farmer(data)["errors"])
        self.mock_helper.assert_not_called()

    def test_non_object_input(self):
        for value in (None, [], "text"):
            self.assertEqual(register_farmer(value)["code"], "validation_error")
        self.mock_helper.assert_not_called()

    def test_optional_location(self):
        self.data.pop("location")
        self.assertTrue(register_farmer(self.data)["ok"])
        self.assertEqual(self.cursor.execute.call_args_list[2].args[1], (41, None))

    def test_password_spaces_preserved(self):
        self.data.update(password="  password  ", password_confirmation="  password  ")
        self.assertTrue(register_farmer(self.data)["ok"])
        stored = self.cursor.execute.call_args_list[1].args[1][3]
        self.assertTrue(check_password_hash(stored, "  password  "))
        self.assertFalse(check_password_hash(stored, "password"))

    def test_insert_failures_rollback(self):
        for index in (1, 2):
            with self.subTest(insert=index):
                self.connection.reset_mock()
                self.cursor.execute.side_effect = [None] * index + [Error("private detail")]
                result = register_farmer(self.data)
                self.assertEqual(result["code"], "database_error")
                self.connection.rollback.assert_called_once_with()
                self.connection.commit.assert_not_called()
                self.cursor.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                self.assertNotIn("private detail", str(result))

    def test_profile_integrity_failure_is_not_duplicate_phone(self):
        self.cursor.execute.side_effect = [None, None, IntegrityError(errno=1062)]
        self.assertEqual(register_farmer(self.data)["code"], "database_error")
        self.connection.rollback.assert_called_once_with()

    def test_commit_failure_rollback(self):
        self.connection.commit.side_effect = Error("private detail")
        self.assertEqual(register_farmer(self.data)["code"], "database_error")
        self.connection.rollback.assert_called_once_with()

    def test_connection_failure(self):
        self.mock_helper.side_effect = Error("private detail")
        result = register_farmer(self.data)
        self.assertEqual(result["code"], "database_error")
        self.assertNotIn("private detail", str(result))
        self.connection.commit.assert_not_called()

    def test_transaction_start_failure(self):
        self.connection.start_transaction.side_effect = Error("private detail")
        self.assertEqual(register_farmer(self.data)["code"], "database_error")
        self.connection.rollback.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_cleanup_failure_does_not_expose_details(self):
        self.cursor.close.side_effect = Error("private detail")
        self.connection.close.side_effect = Error("private detail")
        self.assertTrue(register_farmer(self.data)["ok"])

    def test_json_route_success(self):
        app = create_app()
        app.config["TESTING"] = True
        response = app.test_client().post("/farmer/register", json=self.data)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json["ok"])

    def test_json_route_error_statuses(self):
        client = create_app().test_client()
        self.assertEqual(client.post("/farmer/register", data=self.data).status_code, 415)
        self.assertEqual(client.post("/farmer/register", data="{", content_type="application/json").status_code, 400)
        self.assertEqual(client.post("/farmer/register", json=[]).status_code, 400)
        self.mock_helper.assert_not_called()
        self.cursor.fetchone.return_value = (41,)
        self.assertEqual(client.post("/farmer/register", json=self.data).status_code, 409)
        self.mock_helper.side_effect = Error("private detail")
        self.assertEqual(client.post("/farmer/register", json=self.data).status_code, 503)

    def test_hashing_failure_is_logged_as_server_error(self):
        # Sensitive exception text must never appear in responses or log records.
        with patch(
            "app.services.registration_service.generate_password_hash",
            side_effect=RuntimeError("private-password private-hash private-phone"),
        ), self.assertLogs("app.services.registration_service", level="ERROR") as logs:
            result = register_farmer(self.data)
        self.assertEqual(result["code"], "internal_error")
        self.mock_helper.assert_not_called()
        self.assertIn("password hashing", logs.output[0])
        self.assertNotIn("private-", str(result) + str(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_unexpected_failures_rollback_and_release_resources(self):
        # Unexpected Python errors during either INSERT still roll back the transaction.
        for index in (1, 2):
            with self.subTest(insert=index):
                self.connection.reset_mock()
                self.cursor.execute.side_effect = [None] * index + [RuntimeError("private-detail")]
                with self.assertLogs("app.services.registration_service", level="ERROR") as logs:
                    result = register_farmer(self.data)
                self.assertEqual(result["code"], "internal_error")
                self.connection.rollback.assert_called_once_with()
                self.connection.commit.assert_not_called()
                self.cursor.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                self.assertNotIn("private-detail", str(result) + str(logs.output))

    def test_database_failures_are_logged_without_sensitive_values(self):
        sensitive_hash = "private-password-hash"
        with patch(
            "app.services.registration_service.generate_password_hash",
            return_value=sensitive_hash,
        ):
            self.cursor.execute.side_effect = [None, None, Error(
                "private-database-credentials " + self.data["password"]
                + " " + self.data["phone_number"] + " " + sensitive_hash
            )]
            with self.assertLogs("app.services.registration_service", level="ERROR") as logs:
                result = register_farmer(self.data)
        self.assertEqual(result["code"], "database_error")
        self.assertIn("farmer profile creation", logs.output[0])
        for sensitive in (self.data["password"], self.data["phone_number"], sensitive_hash, "private-database-credentials"):
            self.assertNotIn(sensitive, str(result) + str(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    def test_rollback_and_cleanup_failures_are_logged(self):
        self.cursor.execute.side_effect = [None, Error("private-detail")]
        self.connection.rollback.side_effect = RuntimeError("private-detail")
        self.cursor.close.side_effect = RuntimeError("private-detail")
        self.connection.close.side_effect = RuntimeError("private-detail")
        with self.assertLogs("app.services.registration_service", level="WARNING") as logs:
            result = register_farmer(self.data)
        self.assertEqual(result["code"], "database_error")
        self.assertTrue(any("rollback failed" in line for line in logs.output))
        self.assertTrue(any("cursor cleanup failed" in line for line in logs.output))
        self.assertTrue(any("connection cleanup failed" in line for line in logs.output))
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private-detail", str(result) + str(logs.output))

    def test_unexpected_failure_http_status(self):
        self.mock_helper.side_effect = RuntimeError("private-detail")
        with self.assertLogs("app.services.registration_service", level="ERROR"):
            response = create_app().test_client().post("/farmer/register", json=self.data)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["code"], "internal_error")
        self.assertNotIn("private-detail", str(response.json))

    def test_unknown_service_error_is_not_validation_error(self):
        with patch("app.routes.farmer.register_farmer", return_value={
            "ok": False, "code": "unknown_failure", "errors": {"request": "Failed."}
        }):
            response = create_app().test_client().post("/farmer/register", json=self.data)
        self.assertEqual(response.status_code, 500)
        self.mock_helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
