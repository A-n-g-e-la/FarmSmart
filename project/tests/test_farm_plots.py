import time
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.farm_plot_service import get_farmer_plots


class FarmPlotServiceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchall.return_value = [{
            "plot_id": 12, "plot_name": "North plot", "area_acres": Decimal("1.250000"),
            "location": None, "soil_type": "Loam", "farming_type": None,
            "farmer_id": 91, "created_at": "not-for-response",
        }]
        helper = patch("app.services.farm_plot_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.live_connector.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.start_transaction.assert_not_called()

    def test_read_is_parameterized_owned_ordered_and_allowlisted(self):
        result = get_farmer_plots(91)
        self.assertEqual(result, {"ok": True, "plots": [{
            "plot_id": 12, "plot_name": "North plot", "area_acres": "1.250000",
            "location": None, "soil_type": "Loam", "farming_type": None,
        }]})
        self.connection.cursor.assert_called_once_with(dictionary=True)
        sql, params = self.cursor.execute.call_args.args
        self.assertTrue(sql.startswith("SELECT plot_id, plot_name, area_acres, location, soil_type, farming_type "))
        self.assertIn("WHERE farmer_id = %s ORDER BY plot_id ASC", sql)
        self.assertEqual(params, (91,))
        self.cursor.execute.assert_called_once()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_empty_list_is_success(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(get_farmer_plots(91), {"ok": True, "plots": []})

    def test_invalid_identity_never_opens_connection(self):
        for value in (None, True, 0, -1, "91", "91 OR 1=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_farmer_plots(value)
        self.get_connection.assert_not_called()

    def test_connection_exception_is_safe(self):
        self.get_connection.side_effect = Error("private connection credentials")
        with self.assertLogs("app.services.farm_plot_service", level="ERROR") as logs:
            result = get_farmer_plots(91)
        self.assertEqual(result["code"], "database_error")
        self.assertFalse(result["ok"])
        self.assertNotIn("plots", result)
        self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.close.assert_not_called()

    def test_unavailable_connection_is_safe(self):
        self.get_connection.return_value = None
        with self.assertLogs("app.services.farm_plot_service", level="ERROR"):
            self.assertEqual(get_farmer_plots(91)["code"], "database_error")

    def test_cursor_failure_still_closes_connection(self):
        self.connection.cursor.side_effect = Error("private cursor details")
        with self.assertLogs("app.services.farm_plot_service", level="ERROR"):
            self.assertEqual(get_farmer_plots(91)["code"], "database_error")
        self.connection.close.assert_called_once_with()

    def test_query_and_fetch_errors_are_not_empty_successes(self):
        for method in (self.cursor.execute, self.cursor.fetchall):
            with self.subTest(method=method):
                method.side_effect = Error("private SQL details")
                with self.assertLogs("app.services.farm_plot_service", level="ERROR") as logs:
                    result = get_farmer_plots(91)
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("plots", result)
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.assertTrue(self.cursor.close.called)
                self.assertTrue(self.connection.close.called)
                method.side_effect = None

    def test_cleanup_failure_does_not_leak_or_prevent_connection_close(self):
        self.cursor.close.side_effect = Error("private cleanup details")
        with self.assertLogs("app.services.farm_plot_service", level="WARNING") as logs:
            result = get_farmer_plots(91)
        self.assertTrue(result["ok"])
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private", str(logs.output))


class FarmerPlotEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-plot-test-key")
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
        # Fake SQL returns only the rows belonging to the bound owner.
        self.records = [{
            "farmer_id": owner, "plot_id": plot, "plot_name": name,
            "area_acres": Decimal("0.750000"), "location": None,
            "soil_type": None, "farming_type": None,
        } for owner, plot, name in ((91, 12, "North plot"), (92, 13, "South plot"))]
        self.cursor.fetchall.side_effect = lambda: [
            row for row in self.records
            if row["farmer_id"] == self.cursor.execute.call_args.args[1][0]
        ]
        helper = patch("app.services.farm_plot_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.live_connector.assert_not_called()
        self.connection.commit.assert_not_called()

    def sign_in(self, client=None, user_id=41, farmer_id=91):
        with (client or self.client).session_transaction() as session:
            session.update(user_id=user_id, farmer_id=farmer_id, authenticated_at=time.time())

    def test_authenticated_retrieval_and_client_ids_are_ignored(self):
        self.sign_in()
        response = self.client.get(
            "/farmer/plots?farmer_id=92&user_id=42&plot_id=13",
            headers={"X-Farmer-ID": "92"}, json={"farmer_id": 92},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.is_json)
        self.assertEqual(set(response.json), {"ok", "plots"})
        self.assertEqual([plot["plot_id"] for plot in response.json["plots"]], [12])
        self.assertEqual(response.json["plots"][0]["area_acres"], "0.750000")
        self.assertNotIn("farmer_id", response.json["plots"][0])
        self.lookup.assert_called_once_with(41, 91)
        self.assertEqual(self.cursor.execute.call_args.args[1], (91,))

    def test_two_farmers_receive_only_their_own_plots(self):
        other = self.app.test_client()
        self.sign_in()
        self.sign_in(other, user_id=42, farmer_id=92)
        for client, owner, expected in ((self.client, 91, 12), (other, 92, 13)):
            response = client.get("/farmer/plots?farmer_id=999")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([plot["plot_id"] for plot in response.json["plots"]], [expected])
            self.assertEqual(self.cursor.execute.call_args.args[1], (owner,))

    def test_view_uses_revalidated_identity(self):
        self.sign_in()
        self.lookup.side_effect = None
        self.lookup.return_value = {"ok": True, "identity": {"user_id": 41, "farmer_id": 92}}
        self.client.get("/farmer/plots")
        self.assertEqual(self.cursor.execute.call_args.args[1], (92,))

    def test_empty_list(self):
        self.sign_in(farmer_id=93)
        response = self.client.get("/farmer/plots")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok": True, "plots": []})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_unauthenticated_and_expired_sessions_never_query_plots(self):
        response = self.client.get("/farmer/plots?farmer_id=91")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.sign_in()
        with self.client.session_transaction() as session:
            session["authenticated_at"] = time.time() - 1801
        response = self.client.get("/farmer/plots")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.lookup.assert_not_called()
        self.get_connection.assert_not_called()

    def test_invalidated_identity_and_authorization_outage_fail_closed(self):
        self.lookup.side_effect = None
        for code, status in (("invalid_session", 401), ("authentication_error", 503)):
            with self.subTest(code=code):
                self.sign_in()
                self.lookup.return_value = {"ok": False, "code": code}
                response = self.client.get("/farmer/plots")
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("plots", response.json)
        self.get_connection.assert_not_called()

    def test_connection_failure_is_503_without_clearing_valid_session(self):
        self.sign_in()
        self.get_connection.side_effect = Error("private credentials")
        with self.assertLogs("app.services.farm_plot_service", level="ERROR"):
            response = self.client.get("/farmer/plots")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json["code"], "database_error")
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertNotIn("plots", response.json)
        with self.client.session_transaction() as session:
            self.assertEqual(session["farmer_id"], 91)

    def test_editing_and_deletion_are_not_supported(self):
        self.sign_in()
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)("/farmer/plots").status_code, 405)
        self.get_connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
