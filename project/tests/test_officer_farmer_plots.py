import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.officer_farmer_service import get_assigned_farmer_plots


class OfficerFarmerPlotTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-officer-plot-key")
        self.client = self.app.test_client()
        self.lookup_patcher = patch("app.security.get_active_officer_identity", side_effect=lambda user, officer: {
            "ok": True, "identity": {"user_id": user, "agricultural_officer_id": officer},
        })
        self.lookup = self.lookup_patcher.start()
        self.addCleanup(self.lookup_patcher.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.farmers = {
            farmer: {"assigned_officer_id": officer, "role": role, "status": status}
            for farmer, officer, role, status in (
                (101, 91, "farmer", "active"), (102, 92, "farmer", "active"),
                (103, None, "farmer", "active"), (104, 91, "farmer", "active"),
                (105, 91, "farmer", "pending"), (106, 91, "farmer", "disabled"),
                (107, 91, "officer", "active"))
        }
        self.records = [{
            "farmer_id": farmer, "plot_id": plot, "plot_name": name,
            "area_acres": Decimal("1.234000"), "location": None, "soil_type": "Loam", "farming_type": "Mixed practice",
            "password_hash": "private-hash", "user_id": 999, "phone_number": "private-phone", "created_at": "private-date",
        } for farmer, plot, name in ((101, 11, "North"), (101, 12, "South"), (102, 13, "Other officer's plot"),
                                    (103, 14, "Unassigned plot"), (105, 15, "Pending farmer's plot"))]

        def rows():
            farmer_id, officer_id = self.cursor.execute.call_args.args[1]
            farmer = self.farmers.get(farmer_id)
            if (farmer is None or farmer["assigned_officer_id"] != officer_id
                    or farmer["role"] != "farmer" or farmer["status"] != "active"):
                return []
            plots = [row for row in self.records if row["farmer_id"] == farmer_id]
            # Model the farmer-rooted LEFT JOIN's single NULL plot row.
            return plots or [{"farmer_id": farmer_id, "plot_id": None}]

        self.cursor.fetchall.side_effect = rows
        helper = patch("app.services.officer_farmer_service.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.guard.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.start_transaction.assert_not_called()

    def sign_in(self, user=41, officer=91):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=user, agricultural_officer_id=officer, authenticated_at=time.time())

    def test_assigned_farmer_multiple_plots_are_serialized_and_allowlisted(self):
        self.sign_in()
        response = self.client.get("/officer/api/farmers/101/plots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json, {"ok": True, "farmer_id": 101, "plots": [{
            "plot_id": plot, "plot_name": name, "area_acres": "1.234000", "location": None,
            "soil_type": "Loam", "farming_type": "Mixed practice",
        } for plot, name in ((11, "North"), (12, "South"))]})
        self.lookup.assert_called_once_with(41, 91)
        for forbidden in ("private", "password", "user_id", "phone_number", "created_at", "Other officer", "Unassigned plot"):
            self.assertNotIn(forbidden, response.get_data(as_text=True))
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_single_parameterized_query_checks_requested_farmer_assignment_and_active_role(self):
        self.assertTrue(get_assigned_farmer_plots(91, 101)["ok"])
        self.cursor.execute.assert_called_once()
        sql, params = self.cursor.execute.call_args.args
        self.assertEqual(params, (101, 91))
        self.assertIn("WHERE f.farmer_id = %s AND f.assigned_officer_id = %s", sql)
        self.assertIn("JOIN users AS u ON u.user_id = f.user_id", sql)
        self.assertIn("LEFT JOIN farm_plots AS p ON p.farmer_id = f.farmer_id", sql)
        self.assertIn("AND u.role = 'farmer' AND u.status = 'active'", sql)
        self.assertIn("ORDER BY p.plot_id ASC", sql)
        for forbidden in ("SELECT *", "password", "email", "phone_number", "FOR UPDATE"):
            self.assertNotIn(forbidden, sql)

    def test_assigned_farmer_without_plots_has_empty_array_not_404(self):
        self.sign_in()
        response = self.client.get("/officer/api/farmers/104/plots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "farmer_id": 104, "plots": []})

    def test_missing_unassigned_other_officer_and_inactive_farmers_share_identical_404(self):
        self.sign_in()
        expected = {"ok": False, "code": "not_found", "message": "Farmer not found."}
        for farmer in (999, 103, 102, 105, 106, 107, 0):
            with self.subTest(farmer=farmer):
                response = self.client.get(f"/officer/api/farmers/{farmer}/plots")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json, expected)
                self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_other_officer_can_only_read_its_own_assigned_farmers_plots(self):
        self.sign_in(user=42, officer=92)
        self.assertEqual(self.client.get("/officer/api/farmers/101/plots").status_code, 404)
        response = self.client.get("/officer/api/farmers/102/plots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([plot["plot_id"] for plot in response.json["plots"]], [13])
        self.assertEqual(self.cursor.execute.call_args.args[1], (102, 92))

    def test_client_query_body_and_header_ids_cannot_override_path_or_validated_officer(self):
        self.sign_in()
        response = self.client.get("/officer/api/farmers/101/plots?farmer_id=102&agricultural_officer_id=92&officer_id=92",
                                   headers={"X-Officer-ID": "92"}, json={"farmer_id": 102, "officer_id": 92})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["farmer_id"], 101)
        self.assertEqual(self.cursor.execute.call_args.args[1], (101, 91))
        response = self.client.get("/officer/api/farmers/102/plots?farmer_id=101&agricultural_officer_id=92")
        self.assertEqual(response.status_code, 404)

    def test_injection_and_non_integer_path_ids_never_reach_retrieval(self):
        self.sign_in()
        for farmer in ("101%20OR%201=1", "-1", "abc", "1.5"):
            self.assertEqual(self.client.get(f"/officer/api/farmers/{farmer}/plots").status_code, 404)
        self.helper.assert_not_called()

    def test_invalid_service_ids_never_open_database(self):
        for officer in (None, True, "91", 0, -1):
            with self.assertRaises(ValueError):
                get_assigned_farmer_plots(officer, 101)
        for farmer in (None, True, "101 OR 1=1", 0, -1):
            self.assertEqual(get_assigned_farmer_plots(91, farmer)["code"], "not_found")
        self.helper.assert_not_called()

    def test_anonymous_farmer_expired_and_malformed_sessions_cannot_read_plots(self):
        for identity in ({}, {"user_id": 51, "farmer_id": 101, "authenticated_at": time.time()},
                         {"user_id": 41, "agricultural_officer_id": 91, "authenticated_at": time.time() - 1801},
                         {"user_id": 41, "agricultural_officer_id": "91", "authenticated_at": time.time()}):
            with self.client.session_transaction() as session:
                session.clear()
                session.update(identity)
            response = self.client.get("/officer/api/farmers/101/plots")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("plots", response.json)
        self.lookup.assert_not_called()
        self.helper.assert_not_called()

    def test_disabled_unapproved_missing_profile_or_wrong_role_officers_are_rejected(self):
        self.lookup_patcher.stop()
        account = {"role": "officer", "status": "active", "approved": True, "profile": True}
        for changes in ({"status": "disabled"}, {"status": "pending"}, {"approved": False},
                        {"profile": False}, {"role": "farmer"}):
            with self.subTest(changes=changes):
                current = dict(account, **changes)
                auth_connection = MagicMock()
                auth_cursor = auth_connection.cursor.return_value

                def execute(sql, params):
                    self.assertIn("JOIN agricultural_officers AS o ON o.user_id = u.user_id", sql)
                    self.assertIn("u.role = 'officer' AND u.status = 'active'", sql)
                    self.assertIn("o.approved_at IS NOT NULL", sql)
                    self.assertEqual(params, (41, 91))
                    eligible = current["role"] == "officer" and current["status"] == "active" and current["approved"] and current["profile"]
                    auth_cursor.fetchone.return_value = {"user_id": 41, "agricultural_officer_id": 91} if eligible else None

                auth_cursor.execute.side_effect = execute
                self.sign_in()
                with patch("app.services.officer_authentication_service.get_db_connection", return_value=auth_connection):
                    response = self.client.get("/officer/api/farmers/101/plots")
                self.assertEqual(response.status_code, 401)
                self.assertNotIn("plots", response.json)
                with self.client.session_transaction() as session:
                    self.assertFalse(dict(session))
        self.helper.assert_not_called()

    def test_authorization_outage_is_safe_503_and_never_queries_plots(self):
        self.sign_in()
        self.lookup.side_effect = None
        self.lookup.return_value = {"ok": False, "code": "authentication_error"}
        response = self.client.get("/officer/api/farmers/101/plots")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("plots", response.json)
        self.helper.assert_not_called()

    def test_connection_exception_and_unavailable_connection_return_sanitized_503(self):
        self.sign_in()
        for unavailable in (False, True):
            self.helper.side_effect = None if unavailable else Error("private credentials")
            self.helper.return_value = None
            with self.assertLogs("app.services.officer_farmer_service", level="ERROR") as logs:
                response = self.client.get("/officer/api/farmers/101/plots")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json["code"], "database_error")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("private", str(response.json) + str(logs.output))
            self.assertNotIn("plots", response.json)

    def test_cursor_query_and_fetch_errors_close_resources_without_partial_data(self):
        self.sign_in()
        for stage in ("cursor", "execute", "fetchall"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                target = self.connection.cursor if stage == "cursor" else getattr(self.cursor, stage)
                original = target.side_effect
                target.side_effect = Error("private SQL")
                with self.assertLogs("app.services.officer_farmer_service", level="ERROR") as logs:
                    response = self.client.get("/officer/api/farmers/101/plots")
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("plots", response.json)
                self.assertNotIn("private", str(response.json) + str(logs.output))
                self.connection.close.assert_called_once_with()
                if stage != "cursor":
                    self.cursor.close.assert_called_once_with()
                target.side_effect = original

    def test_cleanup_failure_does_not_expose_diagnostics_or_skip_connection_close(self):
        self.cursor.close.side_effect = Error("private")
        self.connection.close.side_effect = Error("private")
        with self.assertLogs("app.services.officer_farmer_service", level="WARNING") as logs:
            self.assertTrue(get_assigned_farmer_plots(91, 101)["ok"])
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private", str(logs.output))

    def test_write_methods_are_rejected_without_plot_queries(self):
        self.sign_in()
        for method in ("post", "put", "patch", "delete"):
            response = getattr(self.client, method)("/officer/api/farmers/101/plots", json={"plot_name": "Attempt"})
            self.assertEqual(response.status_code, 405)
        self.helper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
