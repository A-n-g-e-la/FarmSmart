import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import dotenv
from mysql.connector import Error, IntegrityError

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.farm_plot_service import register_farmer_plot


class PlotRegistrationServiceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.lastrowid = 123
        helper = patch("app.services.farm_plot_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)
        self.data = {"plot_name": " North plot ", "area_acres": "1.250"}

    def tearDown(self):
        self.live_connector.assert_not_called()

    def test_creation_uses_bound_owner_and_generated_id_then_commits(self):
        self.data.update(farmer_id=999, plot_id=999, location=" Moiben ",
                         soil_type=" Loam ", farming_type=" Rainfed ")
        result = register_farmer_plot(91, self.data)
        self.assertEqual(result, {"ok": True, "plot_id": 123,
                                  "message": "Farm plot registered successfully."})
        sql, params = self.cursor.execute.call_args.args
        self.assertEqual(sql, "INSERT INTO farm_plots "
                         "(farmer_id, plot_name, area_acres, location, soil_type, farming_type) "
                         "VALUES (%s, %s, %s, %s, %s, %s)")
        self.assertEqual(params, (91, "North plot", Decimal("1.250"), "Moiben", "Loam", "Rainfed"))
        self.assertEqual(self.connection.mock_calls, [
            call.start_transaction(), call.cursor(), call.cursor().execute(sql, params),
            call.commit(), call.cursor().close(), call.close(),
        ])
        self.connection.rollback.assert_not_called()

    def test_optional_fields_become_null_and_names_are_not_interpolated(self):
        self.data.update(plot_name="x'); DROP TABLE farm_plots; --", location="  ",
                         farming_type=None, soil_type="")
        self.assertTrue(register_farmer_plot(91, self.data)["ok"])
        sql, params = self.cursor.execute.call_args.args
        self.assertNotIn(self.data["plot_name"], sql)
        self.assertEqual(params[1], self.data["plot_name"])
        self.assertEqual(params[3:], (None, None, None))

    def test_required_name_and_text_type_and_length_validation(self):
        for field, invalid_values in {
            "plot_name": [None, "", "  ", 123, True, [], {}, "x" * 101],
            "location": [123, False, [], {}, "x" * 151],
            "soil_type": [123, False, [], {}, "x" * 101],
            "farming_type": [123, False, [], {}, "x" * 101],
        }.items():
            for value in invalid_values:
                with self.subTest(field=field, value=value):
                    result = register_farmer_plot(91, dict(self.data, **{field: value}))
                    self.assertEqual(result["code"], "validation_error")
                    self.assertIn(field, result["errors"])
        self.get_connection.assert_not_called()

    def test_area_rejects_invalid_types_range_and_precision_before_connecting(self):
        for value in (None, True, False, [], {}, "", " ", "-1", -1, 0, "0.000",
                      "0.0001", "0.000999", "1.2345", "1.2500", "1.250000", 1.2345, "1000000", 1000000,
                      "999999.9999", "NaN", "Infinity", float("nan"), float("inf"),
                      "1e3", "1,250", "1_000", "1.2.3", "1 OR 1=1", "１.２", "1" * 100):
            with self.subTest(value=value):
                result = register_farmer_plot(91, dict(self.data, area_acres=value))
                self.assertEqual(result["code"], "validation_error")
                self.assertIn("area_acres", result["errors"])
        self.get_connection.assert_not_called()

    def test_area_boundaries_numeric_input_and_text_limits_are_accepted(self):
        for value in ("0.001", "999999.999", "100000", 1, 1.25, " 1.250 "):
            with self.subTest(value=value):
                data = dict(self.data, area_acres=value, plot_name="n" * 100,
                            location="l" * 150, soil_type="s" * 100, farming_type="f" * 100)
                self.assertTrue(register_farmer_plot(91, data)["ok"])
                self.assertEqual(self.cursor.execute.call_args.args[1][2], Decimal(str(value).strip()))

    def test_non_objects_and_missing_fields_are_rejected_without_database(self):
        for data in (None, [], "text", 1, {}, {"plot_name": "Name"}, {"area_acres": "1"}):
            with self.subTest(data=data):
                self.assertEqual(register_farmer_plot(91, data)["code"], "validation_error")
        self.get_connection.assert_not_called()

    def test_invalid_server_identity_never_connects(self):
        for owner in (None, "91", True, 0, -1):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                register_farmer_plot(owner, self.data)
        self.get_connection.assert_not_called()

    def test_unavailable_connection_and_connection_exception_are_safe(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                self.get_connection.return_value = None
                self.get_connection.side_effect = Error("private credentials") if unavailable else None
                with self.assertLogs("app.services.farm_plot_service", level="ERROR") as logs:
                    result = register_farmer_plot(91, self.data)
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("plot_id", result)
                self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.commit.assert_not_called()

    def test_transaction_cursor_insert_and_commit_failures_rollback_and_close(self):
        for stage in ("start_transaction", "cursor", "insert", "commit"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                failing = self.cursor.execute if stage == "insert" else getattr(self.connection, stage)
                failing.side_effect = IntegrityError("private constraint") if stage == "insert" else Error("private SQL")
                with self.assertLogs("app.services.farm_plot_service", level="ERROR") as logs:
                    result = register_farmer_plot(91, self.data)
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("plot_id", result)
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.connection.rollback.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                if stage != "commit":
                    self.connection.commit.assert_not_called()
                if stage in ("insert", "commit"):
                    self.cursor.close.assert_called_once_with()
                failing.side_effect = None

    def test_rollback_and_close_failures_preserve_safe_result(self):
        self.cursor.execute.side_effect = Error("private SQL")
        self.connection.rollback.side_effect = Error("private rollback")
        self.cursor.close.side_effect = Error("private cleanup")
        self.connection.close.side_effect = Error("private close")
        with self.assertLogs("app.services.farm_plot_service", level="WARNING") as logs:
            result = register_farmer_plot(91, self.data)
        self.assertFalse(result["ok"])
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private", str(result) + str(logs.output))

    def test_cleanup_failure_after_commit_does_not_claim_insert_failed(self):
        self.cursor.close.side_effect = Error("private close")
        with self.assertLogs("app.services.farm_plot_service", level="WARNING"):
            self.assertTrue(register_farmer_plot(91, self.data)["ok"])
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()
        self.connection.close.assert_called_once_with()


class PlotRegistrationEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-plot-registration-key")
        self.client = self.app.test_client()
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        lookup = patch("app.security.get_active_farmer_identity", return_value={
            "ok": True, "identity": {"user_id": 41, "farmer_id": 91},
        })
        self.lookup = lookup.start()
        self.addCleanup(lookup.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.lastrowid = 123
        helper = patch("app.services.farm_plot_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)
        self.data = {"plot_name": "North plot", "area_acres": "1.250"}

    def tearDown(self):
        self.live_connector.assert_not_called()

    def sign_in(self, client=None):
        with (client or self.client).session_transaction() as session:
            session.update(user_id=41, farmer_id=91, authenticated_at=time.time())

    def token(self):
        response = self.client.get("/farmer/plots/csrf-token")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.json["csrf_token"]

    def test_creation_ignores_client_ownership_and_returns_only_generated_id(self):
        self.sign_in()
        # Prove that the view uses revalidated identity, not an unverified payload.
        self.lookup.return_value = {"ok": True, "identity": {"user_id": 41, "farmer_id": 92}}
        response = self.client.post("/farmer/plots?farmer_id=999&plot_id=456",
                                    json=dict(self.data, farmer_id=999, plot_id=456),
                                    headers={"X-CSRF-Token": self.token(), "X-Farmer-ID": "999"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json, {"ok": True, "plot_id": 123,
                                         "message": "Farm plot registered successfully."})
        self.assertEqual(self.cursor.execute.call_args.args[1][0], 92)
        self.connection.commit.assert_called_once_with()

    def test_form_submission_supports_scoped_token_field(self):
        self.sign_in()
        response = self.client.post("/farmer/plots", data=dict(self.data, csrf_token=self.token()))
        self.assertEqual(response.status_code, 201)

    def test_dashboard_to_creation_and_refreshed_listing_with_mocked_sql(self):
        self.sign_in()
        home = self.client.get("/farmer/home")
        self.assertEqual(home.status_code, 200)
        self.assertIn('id="plot-form"', home.get_data(as_text=True))
        self.get_connection.assert_not_called()
        token = self.token()
        response = self.client.post("/farmer/plots", json=dict(self.data, farming_type="Mixed practice"),
                                    headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 201)
        self.cursor.fetchall.return_value = [{
            "plot_id": 123, "plot_name": "North plot", "area_acres": Decimal("1.250000"),
            "location": None, "soil_type": None, "farming_type": "Mixed practice",
        }]
        listing = self.client.get("/farmer/plots")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.headers["Cache-Control"], "no-store")
        self.assertEqual(listing.json["plots"][0]["plot_id"], response.json["plot_id"])
        self.assertEqual(listing.json["plots"][0]["area_acres"], "1.250000")
        self.assertEqual(listing.json["plots"][0]["farming_type"], "Mixed practice")
        self.assertEqual(self.cursor.execute.call_args.args[1], (91,))

    def test_json_numbers_keep_submitted_precision(self):
        self.sign_in()
        headers = {"X-CSRF-Token": self.token()}
        for area in ("1.0000000000000001", "1.2500", "999999.999000000001"):
            response = self.client.post(
                "/farmer/plots", data='{"plot_name":"North plot","area_acres":' + area + '}',
                content_type="application/json", headers=headers,
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("area_acres", response.json["errors"])
        self.get_connection.assert_not_called()
        response = self.client.post("/farmer/plots", json=dict(self.data, area_acres=1.25), headers=headers)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.cursor.execute.call_args.args[1][2], Decimal("1.25"))

    def test_missing_wrong_unicode_and_wrong_scope_tokens_are_forbidden(self):
        self.sign_in()
        token = self.token()
        for value in (None, "", "wrong", "é" * len(token), self.client.get("/farmer/login").json["csrf_token"]):
            with self.subTest(value=value):
                response = self.client.post("/farmer/plots", json=dict(self.data, csrf_token=value))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json["code"], "csrf_error")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.get_connection.assert_not_called()

    def test_csrf_token_is_session_bound_and_expires(self):
        self.sign_in()
        token = self.token()
        other = self.app.test_client()
        self.sign_in(other)
        self.assertEqual(other.post("/farmer/plots", json=self.data,
                                    headers={"X-CSRF-Token": token}).status_code, 403)
        with self.client.session_transaction() as session:
            session["plot_registration_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.post("/farmer/plots", json=self.data,
                                         headers={"X-CSRF-Token": token}).status_code, 403)
        self.assertNotEqual(self.token(), token)
        self.get_connection.assert_not_called()

    def test_csrf_fetch_does_not_query_plots_or_extend_authentication(self):
        self.sign_in()
        with self.client.session_transaction() as session:
            issued = session["authenticated_at"]
        self.token()
        with self.client.session_transaction() as session:
            self.assertEqual(session["authenticated_at"], issued)
        self.get_connection.assert_not_called()

    def test_unauthenticated_and_expired_sessions_are_rejected_on_both_endpoints(self):
        for expired in (False, True):
            if expired:
                self.sign_in()
                with self.client.session_transaction() as session:
                    session["authenticated_at"] = time.time() - 1801
            for method, url in (("post", "/farmer/plots"), ("get", "/farmer/plots/csrf-token")):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.lookup.assert_not_called()
        self.get_connection.assert_not_called()

    def test_inactive_or_wrong_role_and_authorization_outage_prevent_insert(self):
        for code, status in (("invalid_session", 401), ("authentication_error", 503)):
            for method, url in (("post", "/farmer/plots"), ("get", "/farmer/plots/csrf-token")):
                self.sign_in()
                self.lookup.return_value = {"ok": False, "code": code}
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.get_connection.assert_not_called()

    def test_malformed_requests_and_invalid_fields_return_400(self):
        self.sign_in()
        headers = {"X-CSRF-Token": self.token()}
        for data in (None, [], "text", {}, {"plot_name": "Name", "area_acres": "1.2345"}):
            response = self.client.post("/farmer/plots", json=data, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.json["code"], "validation_error")
        response = self.client.post("/farmer/plots", data="{broken", content_type="application/json", headers=headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post(
            "/farmer/plots", data='{"plot_name":"North plot","area_acres":1e999999999999999999999999}',
            content_type="application/json", headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.get_connection.assert_not_called()

    def test_database_failure_returns_safe_503_and_preserves_session(self):
        self.sign_in()
        token = self.token()
        for stage in ("connection", "insert", "commit"):
            failing = {"connection": self.get_connection, "insert": self.cursor.execute,
                       "commit": self.connection.commit}[stage]
            failing.side_effect = Error("private credentials and SQL")
            with self.assertLogs("app.services.farm_plot_service", level="ERROR"):
                response = self.client.post("/farmer/plots", json=self.data, headers={"X-CSRF-Token": token})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.json["code"], "database_error")
            self.assertNotIn("plot_id", response.json)
            self.assertNotIn("private", response.get_data(as_text=True))
            with self.client.session_transaction() as session:
                self.assertEqual(session["farmer_id"], 91)
            failing.side_effect = None


if __name__ == "__main__":
    unittest.main()
