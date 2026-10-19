import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, call, patch

import dotenv
from mysql.connector import Error

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.farm_plot_service import update_farmer_plot


class PlotUpdateServiceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = (12,)
        helper = patch("app.services.farm_plot_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.live_connector.assert_not_called()

    def test_all_fields_are_normalized_parameterized_and_ownership_scoped(self):
        name = "North'); DROP TABLE farm_plots; --"
        result = update_farmer_plot(91, 12, {
            "plot_name": name, "area_acres": " 2.750 ", "location": " Moiben ",
            "soil_type": " Loam ", "farming_type": " Mixed practice ",
        })
        self.assertEqual(result, {"ok": True, "plot_id": 12, "message": "Farm plot updated successfully."})
        select = "SELECT plot_id FROM farm_plots WHERE plot_id = %s AND farmer_id = %s FOR UPDATE"
        update = "UPDATE farm_plots SET plot_name = %s, area_acres = %s, location = %s, soil_type = %s, farming_type = %s WHERE plot_id = %s AND farmer_id = %s"
        self.assertEqual(self.cursor.execute.call_args_list, [
            call(select, (12, 91)),
            call(update, (name, Decimal("2.750"), "Moiben", "Loam", "Mixed practice", 12, 91)),
        ])
        self.assertNotIn(name, update)
        self.assertEqual(self.connection.mock_calls, [
            call.start_transaction(), call.cursor(), call.cursor().execute(select, (12, 91)),
            call.cursor().fetchone(), call.cursor().execute(update, (name, Decimal("2.750"), "Moiben", "Loam", "Mixed practice", 12, 91)),
            call.commit(), call.cursor().close(), call.close(),
        ])
        self.connection.rollback.assert_not_called()

    def test_partial_update_does_not_include_or_overwrite_omitted_columns(self):
        self.assertTrue(update_farmer_plot(91, 12, {"location": "New location"})["ok"])
        self.assertEqual(self.cursor.execute.call_args.args, (
            "UPDATE farm_plots SET location = %s WHERE plot_id = %s AND farmer_id = %s",
            ("New location", 12, 91),
        ))

    def test_optional_fields_can_be_cleared_using_null_or_blank(self):
        result = update_farmer_plot(91, 12, {"location": None, "soil_type": " ", "farming_type": None})
        self.assertTrue(result["ok"])
        self.assertEqual(self.cursor.execute.call_args.args[1], (None, None, None, 12, 91))

    def test_unchanged_values_succeed_even_when_affected_row_count_is_zero(self):
        self.cursor.rowcount = 0
        self.assertTrue(update_farmer_plot(91, 12, {"plot_name": "Existing name"})["ok"])
        self.connection.commit.assert_called_once_with()

    def test_missing_and_unowned_rows_never_execute_update(self):
        self.cursor.fetchone.return_value = None
        result = update_farmer_plot(91, 12, {"plot_name": "New name"})
        self.assertEqual(result, {"ok": False, "code": "not_found", "message": "Farm plot not found."})
        self.cursor.execute.assert_called_once_with(
            "SELECT plot_id FROM farm_plots WHERE plot_id = %s AND farmer_id = %s FOR UPDATE", (12, 91),
        )
        self.connection.commit.assert_not_called()
        self.connection.rollback.assert_called_once_with()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_invalid_fields_are_rejected_before_any_connection(self):
        invalid = {
            "plot_name": [None, "", " ", True, 5, [], {}, "x" * 101],
            "location": [False, 5, [], {}, "x" * 151],
            "soil_type": [False, 5, [], {}, "x" * 101],
            "farming_type": [False, 5, [], {}, "x" * 101],
            "area_acres": [None, True, False, [], {}, "", "0", "-1", "0.0001", "0.000999", "1.2345",
                           "1.2500", "1.250000", "1000000", 1000000, "NaN", "Infinity", float("nan"),
                           float("inf"), "1e3", "1,250", "1_000", "1" * 100],
        }
        for field, candidates in invalid.items():
            for value in candidates:
                with self.subTest(field=field, value=value):
                    result = update_farmer_plot(91, 12, {field: value})
                    self.assertEqual(result["code"], "validation_error")
                    self.assertIn(field, result["errors"])
        self.get_connection.assert_not_called()

    def test_boundaries_and_free_text_types_match_creation_validation(self):
        for value in ("0.001", "999999.999", 1, 1.25, Decimal("2.750")):
            with self.subTest(area=value):
                self.assertTrue(update_farmer_plot(91, 12, {
                    "plot_name": "n" * 100, "area_acres": value, "location": "l" * 150,
                    "soil_type": "s" * 100, "farming_type": "f" * 100,
                })["ok"])
                self.assertEqual(self.cursor.execute.call_args.args[1][1], Decimal(str(value)))

    def test_unknown_fields_and_empty_or_nonobject_updates_are_rejected(self):
        for data in ({}, None, [], "text", {"farmer_id": 999}, {"plot_id": 22},
                     {"created_at": "now"}, {"updated_at": "now"}, {"csrf_token": "token"},
                     {"location = NULL WHERE 1=1 --": "x"}, {"plot_name": "New", "unknown": True}):
            with self.subTest(data=data):
                self.assertEqual(update_farmer_plot(91, 12, data)["code"], "validation_error")
        self.get_connection.assert_not_called()

    def test_invalid_plot_id_never_connects(self):
        for value in (0, -1, "12", True, None, 18446744073709551616):
            self.assertEqual(update_farmer_plot(91, value, {"location": None})["code"], "not_found")
        self.get_connection.assert_not_called()

    def test_invalid_server_identity_never_connects(self):
        for value in (None, True, 0, -1, "91"):
            with self.assertRaises(ValueError):
                update_farmer_plot(value, 12, {"location": None})
        self.get_connection.assert_not_called()

    def test_connection_exception_and_absent_connection_return_safe_errors(self):
        for absent in (False, True):
            self.get_connection.side_effect = None if absent else Error("private credentials")
            self.get_connection.return_value = None
            with self.assertLogs("app.services.farm_plot_service", level="ERROR") as logs:
                result = update_farmer_plot(91, 12, {"location": None})
            self.assertEqual(result["code"], "database_error")
            self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.commit.assert_not_called()

    def test_transaction_lookup_update_and_commit_failures_rollback_and_close(self):
        for stage in ("transaction", "cursor", "select", "fetch", "update", "commit"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                target = {
                    "transaction": self.connection.start_transaction, "cursor": self.connection.cursor,
                    "select": self.cursor.execute, "fetch": self.cursor.fetchone,
                    "update": self.cursor.execute, "commit": self.connection.commit,
                }[stage]
                target.side_effect = [None, Error("private SQL")] if stage == "update" else Error("private SQL")
                with self.assertLogs("app.services.farm_plot_service", level="ERROR") as logs:
                    result = update_farmer_plot(91, 12, {"location": None})
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.connection.rollback.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                if stage not in ("transaction", "cursor"):
                    self.cursor.close.assert_called_once_with()
                if stage != "commit":
                    self.connection.commit.assert_not_called()
                target.side_effect = None

    def test_failed_rollback_and_cleanup_do_not_expose_details(self):
        self.cursor.execute.side_effect = Error("private query")
        self.connection.rollback.side_effect = Error("private rollback")
        self.cursor.close.side_effect = Error("private close")
        with self.assertLogs("app.services.farm_plot_service", level="WARNING") as logs:
            result = update_farmer_plot(91, 12, {"location": None})
        self.assertFalse(result["ok"])
        self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.close.assert_called_once_with()


class PlotUpdateEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-plot-update-key")
        self.client = self.app.test_client()
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        lookup = patch("app.security.get_active_farmer_identity", side_effect=lambda user_id, farmer_id: {
            "ok": True, "identity": {"user_id": user_id, "farmer_id": farmer_id},
        })
        self.lookup = lookup.start()
        self.addCleanup(lookup.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.return_value = (12,)
        helper = patch("app.services.farm_plot_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.live_connector.assert_not_called()

    def sign_in(self, client=None, farmer_id=91):
        with (client or self.client).session_transaction() as session:
            session.update(user_id=41, farmer_id=farmer_id, authenticated_at=time.time())

    def token(self, client=None):
        response = (client or self.client).get("/farmer/plots/csrf-token")
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def test_partial_update_uses_revalidated_owner_and_ignores_owner_headers_and_query(self):
        self.sign_in()
        self.lookup.side_effect = None
        self.lookup.return_value = {"ok": True, "identity": {"user_id": 41, "farmer_id": 92}}
        response = self.client.patch("/farmer/plots/12?farmer_id=999&plot_id=99", json={"area_acres": "2.750", "location": None},
                                     headers={"X-CSRF-Token": self.token(), "X-Farmer-ID": "999"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json, {"ok": True, "plot_id": 12, "message": "Farm plot updated successfully."})
        self.assertEqual(self.cursor.execute.call_args.args[1], (Decimal("2.750"), None, 12, 92))

    def test_two_farmers_cannot_update_each_others_plots(self):
        other = self.app.test_client()
        self.sign_in()
        self.sign_in(other, farmer_id=92)
        owners = {12: 91, 13: 92}
        self.cursor.fetchone.side_effect = lambda: (
            (self.cursor.execute.call_args.args[1][0],)
            if owners.get(self.cursor.execute.call_args.args[1][0]) == self.cursor.execute.call_args.args[1][1]
            else None
        )
        for client, owned, unowned in ((self.client, 12, 13), (other, 13, 12)):
            token = self.token(client)
            for plot_id, expected in ((owned, 200), (unowned, 404), (999, 404)):
                self.cursor.execute.reset_mock()
                response = client.patch(f"/farmer/plots/{plot_id}", json={"plot_name": "Changed"}, headers={"X-CSRF-Token": token})
                self.assertEqual(response.status_code, expected)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                if expected == 404:
                    self.cursor.execute.assert_called_once()
                    self.assertEqual(response.json, {"ok": False, "code": "not_found", "message": "Farm plot not found."})

    def test_missing_wrong_unicode_and_wrong_scope_csrf_tokens_are_forbidden(self):
        self.sign_in()
        token = self.token()
        login_token = self.client.get("/farmer/login").json["csrf_token"]
        for value in ("", "wrong", "é" * len(token), login_token):
            response = self.client.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": value})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json["code"], "csrf_error")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.get_connection.assert_not_called()

    def test_csrf_is_session_bound_and_expires(self):
        self.sign_in()
        token = self.token()
        other = self.app.test_client()
        self.sign_in(other)
        self.assertEqual(other.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": token}).status_code, 403)
        with self.client.session_transaction() as session:
            session["plot_registration_csrf_issued"] = time.time() - 3601
        self.assertEqual(self.client.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": token}).status_code, 403)
        self.get_connection.assert_not_called()

    def test_unauthenticated_expired_and_revoked_sessions_never_update(self):
        self.assertEqual(self.client.patch("/farmer/plots/12", json={"location": None}).status_code, 401)
        self.sign_in()
        token = self.token()
        with self.client.session_transaction() as session:
            session["authenticated_at"] = time.time() - 1801
        response = self.client.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.sign_in()
        self.lookup.side_effect = None
        self.lookup.return_value = {"ok": False, "code": "invalid_session"}
        self.assertEqual(self.client.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": token}).status_code, 401)
        self.get_connection.assert_not_called()

    def test_invalid_json_fields_and_empty_updates_return_400_without_database(self):
        self.sign_in()
        headers = {"X-CSRF-Token": self.token()}
        for data in ({}, [], "text", {"area_acres": None}, {"area_acres": "0.0001"}, {"plot_name": None},
                     {"farmer_id": 999}, {"plot_id": 99}, {"location": None, "unknown": "x"}):
            response = self.client.patch("/farmer/plots/12", json=data, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["code"], "validation_error")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        for payload in ("{broken", '{"area_acres":1.0000000000000001}', '{"area_acres":1e999999999999999999999999}'):
            response = self.client.patch("/farmer/plots/12", data=payload, content_type="application/json", headers=headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.patch("/farmer/plots/12", data={"location": "New"}, headers=headers).status_code, 400)
        self.get_connection.assert_not_called()

    def test_numeric_acreage_remains_decimal_and_optional_null_can_clear(self):
        self.sign_in()
        response = self.client.patch("/farmer/plots/12", json={"area_acres": 1.25, "farming_type": None}, headers={"X-CSRF-Token": self.token()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cursor.execute.call_args.args[1], (Decimal("1.25"), None, 12, 91))

    def test_database_failure_returns_safe_503_and_preserves_session(self):
        self.sign_in()
        token = self.token()
        self.cursor.execute.side_effect = [None, Error("private SQL and credentials")]
        with self.assertLogs("app.services.farm_plot_service", level="ERROR"):
            response = self.client.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "database_error")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("private", response.get_data(as_text=True))
        self.connection.rollback.assert_called_once_with()
        with self.client.session_transaction() as session:
            self.assertEqual(session["farmer_id"], 91)

    def test_authorization_outage_prevents_plot_connection(self):
        self.sign_in()
        token = self.token()
        self.lookup.side_effect = None
        self.lookup.return_value = {"ok": False, "code": "authentication_error"}
        response = self.client.patch("/farmer/plots/12", json={"location": None}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.get_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
