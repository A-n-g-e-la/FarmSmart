import time
import unittest
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error, IntegrityError
from werkzeug.security import check_password_hash

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.officer_registration_service import normalize_officer_email, register_officer


def registration_data():
    return {
        "first_name": " Test ", "last_name": " Officer ", "email": " Officer@Example.COM ",
        "password": "synthetic-password", "password_confirmation": "synthetic-password",
        "region": " Uasin Gishu ", "organization": " Extension Service ",
    }


class OfficerRegistrationServiceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = None

        def execute(sql, params):
            self.cursor.lastrowid = 91 if sql.startswith("INSERT INTO agricultural_officers") else 41

        self.cursor.execute.side_effect = execute
        helper = patch("app.services.officer_registration_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)
        self.data = registration_data()

    def tearDown(self):
        self.live_connector.assert_not_called()

    def test_success_is_pending_with_scrypt_and_correct_profile_linkage(self):
        result = register_officer(self.data)
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["status"], "pending")
        self.assertIn("Approval", result["message"])
        self.assertNotIn("user_id", result)
        self.assertNotIn("agricultural_officer_id", result)
        calls = self.cursor.execute.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0].args, ("SELECT user_id FROM users WHERE email = %s LIMIT 1", ("officer@example.com",)))
        user_sql, user_params = calls[1].args
        self.assertEqual(user_sql, "INSERT INTO users (first_name, last_name, email, password_hash, role, status) VALUES (%s, %s, %s, %s, 'officer', 'pending')")
        self.assertEqual(user_params[:3], ("Test", "Officer", "officer@example.com"))
        self.assertTrue(user_params[3].startswith("scrypt:"))
        self.assertTrue(check_password_hash(user_params[3], self.data["password"]))
        self.assertNotIn(self.data["password"], str(result))
        self.assertNotIn(user_params[3], str(result))
        self.assertEqual(calls[2].args, (
            "INSERT INTO agricultural_officers (user_id, region_assigned, organization, approved_at) VALUES (%s, %s, %s, NULL)",
            (41, "Uasin Gishu", "Extension Service"),
        ))
        self.connection.start_transaction.assert_called_once_with()
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        # Both inserts must precede the transaction's only commit.
        methods = [entry[0] for entry in self.connection.mock_calls]
        self.assertLess(max(index for index, method in enumerate(methods) if method == "cursor().execute"), methods.index("commit"))

    def test_email_normalization_and_supported_address_validation(self):
        self.assertEqual(normalize_officer_email("  Officer.Name+tag@EXAMPLE.COM  "), "officer.name+tag@example.com")
        for email in (None, 123, True, [], "", "no-at", "a@@example.com", "a@example", ".a@example.com",
                      "a.@example.com", "a..b@example.com", "a b@example.com", "a@-example.com", "a@example-.com",
                      "a@exam_ple.com", "a@example..com", "a\r\nb@example.com", '"a"@example.com',
                      "é@example.com", "a@exämple.com", "a" * 65 + "@example.com", "a@" + "b" * 64 + ".com"):
            with self.subTest(email=email):
                self.assertIsNone(normalize_officer_email(email))

    def test_email_and_text_boundaries(self):
        maximum = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 61
        self.assertEqual(len(maximum), 254)
        data = dict(self.data, first_name="f" * 100, last_name="l" * 100,
                    region="r" * 100, organization="o" * 150, email=maximum)
        self.assertTrue(register_officer(data)["ok"])
        self.assertEqual(self.cursor.execute.call_args_list[1].args[1][2], maximum)
        self.assertIsNone(normalize_officer_email(maximum + "d"))

    def test_required_fields_types_lengths_and_confirmation_before_connection(self):
        for field in ("first_name", "last_name", "region", "email", "password", "password_confirmation"):
            missing = dict(self.data)
            del missing[field]
            self.assertIn(field, register_officer(missing)["errors"])
        invalid = {
            "first_name": [None, "", " ", 5, True, [], {}, "x" * 101],
            "last_name": [None, "", " ", 5, True, [], {}, "x" * 101],
            "region": [None, "", " ", 5, True, [], {}, "x" * 101],
            "organization": [5, True, [], {}, "x" * 151],
            "email": [None, "", "bad", 5, [], {}, "a" * 255],
            "password": [None, "", " " * 8, "short", "x" * 1025, 5, [], {}],
            "password_confirmation": [None, "", "different", 5, [], {}],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    result = register_officer(dict(self.data, **{field: value}))
                    self.assertEqual(result["code"], "validation_error")
                    self.assertIn(field, result["errors"])
        self.get_connection.assert_not_called()

    def test_password_boundaries_and_whitespace_match_farmer_policy(self):
        for password in ("12345678", "x" * 1024, "  synthetic-password  "):
            with self.subTest(length=len(password)):
                self.assertTrue(register_officer(dict(self.data, password=password, password_confirmation=password))["ok"])
                password_hash = self.cursor.execute.call_args_list[-2].args[1][3]
                self.assertTrue(check_password_hash(password_hash, password))
                if password.startswith(" "):
                    self.assertFalse(check_password_hash(password_hash, password.strip()))

    def test_optional_organization_can_be_absent_null_or_blank(self):
        for organization in (None, "", " "):
            self.assertTrue(register_officer(dict(self.data, organization=organization))["ok"])
            self.assertIsNone(self.cursor.execute.call_args.args[1][2])
        del self.data["organization"]
        self.assertTrue(register_officer(self.data)["ok"])
        self.assertIsNone(self.cursor.execute.call_args.args[1][2])

    def test_privileged_unknown_fields_and_nonobjects_are_rejected(self):
        for field in ("role", "status", "approved_at", "user_id", "officer_id", "agricultural_officer_id",
                      "assigned_officer_id", "farmer_id", "region_assigned", "unknown", "csrf_token"):
            with self.subTest(field=field):
                self.assertEqual(register_officer(dict(self.data, **{field: "injected"}))["code"], "validation_error")
        for data in (None, [], "text", 5):
            self.assertEqual(register_officer(data)["code"], "validation_error")
        self.get_connection.assert_not_called()

    def test_duplicate_email_precheck_rolls_back_and_exposes_no_account_details(self):
        self.cursor.fetchone.return_value = (999,)
        result = register_officer(self.data)
        self.assertEqual(result["code"], "duplicate_email")
        self.assertNotIn("999", str(result))
        self.assertNotIn("officer@example.com", str(result))
        self.cursor.execute.assert_called_once()
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()

    def test_duplicate_email_race_is_graceful(self):
        for constraint in ("uq_users_email", "users.uq_users_email"):
            for quote in ("'", "`"):
                with self.subTest(constraint=constraint, quote=quote):
                    self.connection.reset_mock()
                    message = f"Duplicate entry 'private' for key {quote}{constraint}{quote}"
                    self.cursor.execute.side_effect = [None, IntegrityError(message, errno=1062)]
                    self.assertEqual(register_officer(self.data), {
                        "ok": False, "code": "duplicate_email", "errors": {
                            "email": "Registration cannot be completed with this email address.",
                        },
                    })
                    self.connection.rollback.assert_called_once_with()
                    self.connection.commit.assert_not_called()
                    self.cursor.close.assert_called_once_with()
                    self.connection.close.assert_called_once_with()

    def test_other_integrity_failures_are_not_mislabeled_duplicates(self):
        for message, errno, at_profile in (
            ("Duplicate entry 'private' for key 'PRIMARY'", 1062, False),
            ("Duplicate entry 'private' for key 'uq_users_phone_number'", 1062, False),
            ("Duplicate entry 'private' for key 'other_table.uq_users_email'", 1062, False),
            ("Duplicate entry 'private' for key 'uq_users_email_other'", 1062, False),
            ("Duplicate entry 'private for key 'uq_users_email'' for key 'PRIMARY'", 1062, False),
            ("Unknown format private", 1062, False),
            ("Duplicate entry 'private' for key 'uq_users_email'", 1452, False),
            ("Duplicate entry 'private' for key 'users.uq_users_email'", 1452, False),
            ("Duplicate entry 'private' for key 'uq_users_email'", 1062, True),
            ("Duplicate entry 'private' for key 'users.uq_users_email'", 1062, True),
        ):
            with self.subTest(message=message, errno=errno, at_profile=at_profile):
                self.cursor.execute.side_effect = [None] * (2 if at_profile else 1) + [IntegrityError(message, errno=errno)]
                with self.assertLogs("app.services.officer_registration_service", level="ERROR") as logs:
                    result = register_officer(self.data)
                self.assertNotIn("private", str(logs.output))
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("private", str(result))
        self.connection.commit.assert_not_called()

    def test_failures_rollback_and_close_without_logging_sensitive_details(self):
        for stage in ("start_transaction", "cursor", "lookup", "user", "profile", "commit"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                if stage in ("lookup", "user", "profile"):
                    target = self.cursor.execute
                    target.side_effect = [None] * {"lookup": 0, "user": 1, "profile": 2}[stage] + [Error("private SQL password")]
                else:
                    target = getattr(self.connection, stage)
                    target.side_effect = Error("private SQL password")
                with self.assertLogs("app.services.officer_registration_service", level="ERROR") as logs:
                    result = register_officer(self.data)
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.connection.rollback.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                if stage != "commit":
                    self.connection.commit.assert_not_called()
                if stage not in ("start_transaction", "cursor"):
                    self.cursor.close.assert_called_once_with()
                target.side_effect = None

    def test_connection_and_hashing_failures_are_safe(self):
        self.get_connection.side_effect = Error("private credentials")
        with self.assertLogs("app.services.officer_registration_service", level="ERROR"):
            self.assertEqual(register_officer(self.data)["code"], "database_error")
        self.get_connection.reset_mock()
        with patch("app.services.officer_registration_service.generate_password_hash", side_effect=RuntimeError("private")):
            with self.assertLogs("app.services.officer_registration_service", level="ERROR"):
                self.assertEqual(register_officer(self.data)["code"], "internal_error")
        self.get_connection.assert_not_called()
        self.connection.commit.assert_not_called()

    def test_failed_rollback_and_cleanup_preserve_safe_results(self):
        self.cursor.execute.side_effect = Error("private")
        self.connection.rollback.side_effect = Error("private")
        self.cursor.close.side_effect = Error("private")
        self.connection.close.side_effect = Error("private")
        with self.assertLogs("app.services.officer_registration_service", level="WARNING") as logs:
            result = register_officer(self.data)
        self.assertFalse(result["ok"])
        self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.close.assert_called_once_with()


class OfficerRegistrationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-officer-registration-key")
        self.client = self.app.test_client()
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = None
        self.cursor.lastrowid = 41
        helper = patch("app.services.officer_registration_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.live_connector.assert_not_called()

    def token(self, client=None):
        response = (client or self.client).get("/officer/register")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(set(response.json), {"ok", "csrf_token"})
        return response.json["csrf_token"]

    def test_public_token_and_json_registration_do_not_authenticate(self):
        token = self.token()
        self.get_connection.assert_not_called()
        response = self.client.post("/officer/register", json=registration_data(), headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json["status"], "pending")
        with self.client.session_transaction() as session:
            for key in ("user_id", "farmer_id", "officer_id", "agricultural_officer_id", "authenticated_at", "officer_registration_csrf"):
                self.assertNotIn(key, session)
        self.assertEqual(self.client.get("/farmer/home").status_code, 401)
        self.assertEqual(self.client.get("/officer/home").status_code, 401)
        self.assertEqual(self.client.post("/officer/register", json=registration_data(), headers={"X-CSRF-Token": token}).status_code, 403)

    def test_csrf_missing_wrong_unicode_and_farmer_scopes_are_rejected(self):
        token = self.token()
        farmer_login = self.client.get("/farmer/login").json["csrf_token"]
        self.client.get("/farmer/registration")
        with self.client.session_transaction() as session:
            farmer_registration = session["registration_csrf"]
        for submitted in ("", "wrong", "é" * len(token), farmer_login, farmer_registration):
            response = self.client.post("/officer/register", json=registration_data(), headers={"X-CSRF-Token": submitted})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json["code"], "csrf_error")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.get_connection.assert_not_called()

    def test_csrf_is_bound_to_session_and_expires(self):
        token = self.token()
        other = self.app.test_client()
        self.assertEqual(other.post("/officer/register", json=registration_data(), headers={"X-CSRF-Token": token}).status_code, 403)
        with self.client.session_transaction() as session:
            session["officer_registration_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.post("/officer/register", json=registration_data(), headers={"X-CSRF-Token": token}).status_code, 403)
        self.assertNotEqual(self.token(), token)
        self.get_connection.assert_not_called()

    def test_json_only_and_malformed_payload_handling(self):
        headers = {"X-CSRF-Token": self.token()}
        self.assertEqual(self.client.post("/officer/register", data=registration_data(), headers=headers).status_code, 415)
        for data in ([], "text", 123, None):
            response = self.client.post("/officer/register", json=data, headers=headers)
            self.assertIn(response.status_code, (400, 415))
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.post("/officer/register", data="{broken", content_type="application/json", headers=headers).status_code, 400)
        for fields in ({}, {"role": "officer"}, {"status": "active"}, {"approved_at": "now"}, {"unknown": "value"}):
            self.assertEqual(self.client.post("/officer/register", json=dict(registration_data(), **fields) if fields else {}, headers=headers).status_code, 400)
        self.get_connection.assert_not_called()

    def test_duplicate_and_operational_error_statuses_are_safe(self):
        headers = {"X-CSRF-Token": self.token()}
        self.cursor.fetchone.return_value = (999,)
        response = self.client.post("/officer/register", json=registration_data(), headers=headers)
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("999", response.get_data(as_text=True))
        self.get_connection.side_effect = Error("private credentials")
        with self.assertLogs("app.services.officer_registration_service", level="ERROR"):
            response = self.client.post("/officer/register", json=registration_data(), headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_existing_farmer_session_and_routes_remain_isolated(self):
        with self.client.session_transaction() as session:
            session.update(user_id=41, farmer_id=91, authenticated_at=123.0, logout_csrf="farmer-token")
        response = self.client.post("/officer/register", json=registration_data(), headers={"X-CSRF-Token": self.token()})
        self.assertEqual(response.status_code, 201)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], 41)
            self.assertEqual(session["farmer_id"], 91)
            self.assertEqual(session["authenticated_at"], 123.0)
            self.assertEqual(session["logout_csrf"], "farmer-token")
            self.assertNotIn("agricultural_officer_id", session)
        urls = {rule.rule: rule.endpoint for rule in self.app.url_map.iter_rules()}
        self.assertEqual(urls["/farmer/register"], "farmer.register")
        self.assertEqual(urls["/officer/register"], "officer.register")
        self.assertEqual(urls["/farmer/home"], "farmer.home")
        self.assertEqual(self.client.post("/farmer/login", json={"csrf_token": self.token()}).status_code, 400)
        self.get_connection.assert_called_once()


if __name__ == "__main__":
    unittest.main()
