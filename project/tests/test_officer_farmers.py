import time
import unittest
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.officer_farmer_service import get_assigned_farmers


class AssignedFarmerServiceTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchall.return_value = [
            {"farmer_id": 101, "first_name": "Test", "last_name": "Farmer", "phone_number": "+254712345678", "plot_count": 0,
             "user_id": 51, "password_hash": "private-hash", "session": "private-session"},
            {"farmer_id": 102, "first_name": "Test", "last_name": "Farmer", "phone_number": None, "plot_count": 3},
        ]
        helper = patch("app.services.officer_farmer_service.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.guard.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.start_transaction.assert_not_called()

    def test_parameterized_assignment_filter_grouping_and_allowlisted_response(self):
        self.assertEqual(get_assigned_farmers(91), {"ok": True, "farmers": [
            {"farmer_id": 101, "name": "Test Farmer", "phone_number": "+254712345678", "plot_count": 0},
            {"farmer_id": 102, "name": "Test Farmer", "phone_number": None, "plot_count": 3},
        ]})
        self.cursor.execute.assert_called_once()
        sql, params = self.cursor.execute.call_args.args
        self.assertEqual(params, (91,))
        self.assertIn("WHERE f.assigned_officer_id = %s", sql)
        self.assertIn("AND u.role = 'farmer'", sql)
        self.assertIn("AND u.status = 'active'", sql)
        self.assertIn("LEFT JOIN farm_plots AS p ON p.farmer_id = f.farmer_id", sql)
        self.assertIn("COUNT(p.plot_id) AS plot_count", sql)
        self.assertNotIn("COUNT(*)", sql)
        self.assertIn("GROUP BY f.farmer_id, u.first_name, u.last_name, u.phone_number", sql)
        self.assertIn("ORDER BY f.farmer_id ASC", sql)
        for forbidden in ("password", "email", "approved_at", "SELECT *"):
            self.assertNotIn(forbidden, sql)
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_no_assigned_farmers_is_success_with_empty_list(self):
        self.cursor.fetchall.return_value = []
        self.assertEqual(get_assigned_farmers(91), {"ok": True, "farmers": []})

    def test_invalid_officer_id_never_connects(self):
        for value in (None, True, "91", "91 OR 1=1", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_assigned_farmers(value)
        self.helper.assert_not_called()

    def test_connection_errors_and_unavailable_connection_are_safe(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                self.helper.side_effect = None if unavailable else Error("private credentials")
                self.helper.return_value = None
                with self.assertLogs("app.services.officer_farmer_service", level="ERROR") as logs:
                    result = get_assigned_farmers(91)
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("farmers", result)
                self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.close.assert_not_called()

    def test_cursor_query_and_fetch_failures_release_resources_without_diagnostics(self):
        for stage in ("cursor", "execute", "fetchall"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                target = self.connection.cursor if stage == "cursor" else getattr(self.cursor, stage)
                target.side_effect = Error("private SQL password")
                with self.assertLogs("app.services.officer_farmer_service", level="ERROR") as logs:
                    result = get_assigned_farmers(91)
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("farmers", result)
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.connection.close.assert_called_once_with()
                if stage != "cursor":
                    self.cursor.close.assert_called_once_with()
                target.side_effect = None

    def test_cleanup_failure_does_not_leak_details_or_prevent_connection_close(self):
        self.cursor.close.side_effect = Error("private cursor")
        self.connection.close.side_effect = Error("private connection")
        with self.assertLogs("app.services.officer_farmer_service", level="WARNING") as logs:
            self.assertTrue(get_assigned_farmers(91)["ok"])
        self.connection.close.assert_called_once_with()
        self.assertNotIn("private", str(logs.output))


class OfficerFarmerEndpointTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-assigned-farmer-test-key")
        self.client = self.app.test_client()
        lookup = patch("app.security.get_active_officer_identity", side_effect=lambda user_id, officer_id: {
            "ok": True, "identity": {"user_id": user_id, "agricultural_officer_id": officer_id},
        })
        self.lookup = lookup.start()
        self.addCleanup(lookup.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.records = [
            {"farmer_id": farmer, "assigned_officer_id": officer, "first_name": "Test", "last_name": f"Farmer {farmer}",
             "phone_number": f"synthetic-phone-{farmer}", "plot_count": count, "password_hash": "private-hash", "user_id": farmer + 1000,
             "role": "farmer", "status": "active"}
            for farmer, officer, count in ((101, 91, 0), (102, 91, 3), (103, 92, 1), (104, None, 2))
        ]
        # Mock database filtering is driven by the bound profile ID, never client input.
        self.cursor.fetchall.side_effect = lambda: [row for row in self.records
            if row["assigned_officer_id"] == self.cursor.execute.call_args.args[1][0]
            and ("AND u.role = 'farmer'" not in self.cursor.execute.call_args.args[0] or row["role"] == "farmer")
            and ("AND u.status = 'active'" not in self.cursor.execute.call_args.args[0] or row["status"] == "active")]
        helper = patch("app.services.officer_farmer_service.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)

    def tearDown(self):
        self.guard.assert_not_called()
        self.connection.commit.assert_not_called()

    def sign_in(self, user_id=41, officer_id=91):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=user_id, agricultural_officer_id=officer_id, authenticated_at=time.time())

    def test_assigned_list_ignores_client_ids_and_excludes_unassigned_or_sensitive_data(self):
        self.sign_in()
        response = self.client.get("/officer/api/farmers?officer_id=92&agricultural_officer_id=92&farmer_id=103",
                                   headers={"X-Officer-ID": "92"}, json={"officer_id": 92})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.lookup.assert_called_once_with(41, 91)
        self.assertEqual(self.cursor.execute.call_args.args[1], (91,))
        self.assertEqual([row["farmer_id"] for row in response.json["farmers"]], [101, 102])
        self.assertEqual([row["plot_count"] for row in response.json["farmers"]], [0, 3])
        self.assertEqual(len(response.json["farmers"]), 2)
        for row in response.json["farmers"]:
            self.assertEqual(set(row), {"farmer_id", "name", "phone_number", "plot_count"})
        for forbidden in ("private-hash", "user_id", "csrf", "synthetic-phone-103", "synthetic-phone-104"):
            self.assertNotIn(forbidden, response.get_data(as_text=True))

    def test_different_officers_receive_only_their_own_assigned_farmers(self):
        for user, officer, expected in ((41, 91, [101, 102]), (42, 92, [103]), (43, 93, [])):
            with self.subTest(officer=officer):
                self.sign_in(user, officer)
                response = self.client.get("/officer/api/farmers?officer_id=91")
                self.assertEqual(response.status_code, 200)
                self.assertEqual([row["farmer_id"] for row in response.json["farmers"]], expected)
                self.assertEqual(self.cursor.execute.call_args.args[1], (officer,))

    def test_pending_disabled_and_wrong_role_assigned_users_are_excluded(self):
        for farmer_id, changes in ((105, {"status": "pending"}), (106, {"status": "disabled"}),
                                   (107, {"role": "officer"})):
            self.records.append(dict(self.records[0], farmer_id=farmer_id,
                                     phone_number=f"synthetic-phone-{farmer_id}", **changes))
        self.sign_in()
        response = self.client.get("/officer/api/farmers")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual([row["farmer_id"] for row in response.json["farmers"]], [101, 102])
        self.assertEqual([row["plot_count"] for row in response.json["farmers"]], [0, 3])
        for farmer_id in (105, 106, 107):
            self.assertNotIn(f"synthetic-phone-{farmer_id}", response.get_data(as_text=True))

    def test_only_pending_or_disabled_assigned_farmers_returns_empty_list(self):
        self.sign_in()
        for status in ("pending", "disabled"):
            with self.subTest(status=status):
                self.records = [dict(self.records[0], status=status)]
                response = self.client.get("/officer/api/farmers")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {"ok": True, "farmers": []})

    def test_route_uses_revalidated_identity_not_session_identity_directly(self):
        self.sign_in()
        # Deliberately distinct mock result proves the route reads g.officer_identity.
        self.lookup.side_effect = None
        self.lookup.return_value = {"ok": True, "identity": {"user_id": 41, "agricultural_officer_id": 92}}
        self.assertEqual(self.client.get("/officer/api/farmers").status_code, 200)
        self.assertEqual(self.cursor.execute.call_args.args[1], (92,))

    def test_anonymous_farmer_expired_and_malformed_sessions_cannot_query_farmers(self):
        for identity in ({}, {"user_id": 51, "farmer_id": 101, "authenticated_at": time.time()},
                         {"user_id": 41, "agricultural_officer_id": 91, "authenticated_at": time.time() - 1801},
                         {"user_id": 41, "agricultural_officer_id": "91", "authenticated_at": time.time()}):
            with self.subTest(identity=identity):
                with self.client.session_transaction() as session:
                    session.clear()
                    session.update(identity)
                response = self.client.get("/officer/api/farmers")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("farmers", response.json)
        self.lookup.assert_not_called()
        self.helper.assert_not_called()

    def test_revoked_officer_or_authentication_outage_never_queries_farmers(self):
        self.lookup.side_effect = None
        for code, expected in (("invalid_session", 401), ("authentication_error", 503)):
            self.sign_in()
            self.lookup.return_value = {"ok": False, "code": code}
            response = self.client.get("/officer/api/farmers")
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("farmers", response.json)
        self.helper.assert_not_called()

    def test_database_failure_is_503_not_an_empty_success(self):
        self.sign_in()
        self.cursor.execute.side_effect = Error("private SQL credentials")
        with self.assertLogs("app.services.officer_farmer_service", level="ERROR"):
            response = self.client.get("/officer/api/farmers")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["code"], "database_error")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("farmers", response.json)
        self.assertNotIn("private", response.get_data(as_text=True))

    def test_endpoint_does_not_support_writes(self):
        self.sign_in()
        for method in ("post", "patch", "put", "delete"):
            self.assertEqual(getattr(self.client, method)("/officer/api/farmers").status_code, 405)
        self.helper.assert_not_called()


class OfficerFarmerAuthorizationIntegrationTests(unittest.TestCase):
    def test_actual_authorization_sql_rejects_inactive_unapproved_and_wrong_profile_officers(self):
        with patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden")) as guard:
            app = create_app()
            app.config.update(TESTING=True, SECRET_KEY="isolated-officer-farmer-integration-key")
            client = app.test_client()
            account = {"user_id": 41, "agricultural_officer_id": 91, "role": "officer", "status": "active", "approved": True}
            for changes in ({}, {"status": "pending"}, {"status": "disabled"}, {"approved": False},
                            {"role": "farmer"}, {"agricultural_officer_id": 92}):
                with self.subTest(changes=changes):
                    current = dict(account, **changes)
                    connection = MagicMock()
                    cursor = connection.cursor.return_value

                    def execute(sql, params):
                        self.assertIn("JOIN agricultural_officers AS o ON o.user_id = u.user_id", sql)
                        self.assertIn("u.role = 'officer' AND u.status = 'active'", sql)
                        self.assertIn("o.approved_at IS NOT NULL", sql)
                        self.assertEqual(params, (41, 91))
                        eligible = current["role"] == "officer" and current["status"] == "active" and current["approved"] and current["agricultural_officer_id"] == 91
                        cursor.fetchone.return_value = {"user_id": 41, "agricultural_officer_id": 91} if eligible else None

                    cursor.execute.side_effect = execute
                    with client.session_transaction() as session:
                        session.clear()
                        session.update(user_id=41, agricultural_officer_id=91, authenticated_at=time.time())
                    with patch("app.services.officer_authentication_service.get_db_connection", return_value=connection), patch("app.routes.officer.get_assigned_farmers", return_value={"ok": True, "farmers": []}) as retrieval:
                        response = client.get("/officer/api/farmers")
                    self.assertEqual(response.status_code, 401 if changes else 200)
                    if changes:
                        retrieval.assert_not_called()
                    else:
                        retrieval.assert_called_once_with(91)
            guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
