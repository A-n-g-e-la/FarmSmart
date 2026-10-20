import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.officer_approval_service import approve_officer


class OfficerApprovalTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.live_connector = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.user = {"user_id": 41, "role": "officer", "status": "pending"}
        self.profile = {"agricultural_officer_id": 91, "user_id": 41, "approved_at": None}
        self.cursor.fetchone.side_effect = lambda: dict(self.user) if self.cursor.execute.call_count == 1 else dict(self.profile)
        self.cursor.rowcount = 1
        helper = patch("app.services.officer_approval_service.get_db_connection", return_value=self.connection)
        self.get_connection = helper.start()
        self.addCleanup(helper.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-approval-test-key")
        self.runner = self.app.test_cli_runner()

    def tearDown(self):
        self.live_connector.assert_not_called()

    def test_success_uses_normalized_email_locked_linked_rows_and_conditional_updates(self):
        result = approve_officer("  Officer@Example.COM  ")
        self.assertTrue(result["ok"])
        self.connection.start_transaction.assert_called_once_with()
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.assertEqual([call.args for call in self.cursor.execute.call_args_list], [
            ("SELECT user_id, role, status FROM users WHERE email = %s FOR UPDATE", ("officer@example.com",)),
            ("SELECT agricultural_officer_id, user_id, approved_at FROM agricultural_officers WHERE user_id = %s FOR UPDATE", (41,)),
            ("UPDATE users SET status = 'active' WHERE user_id = %s AND role = 'officer' AND status = 'pending'", (41,)),
            ("UPDATE agricultural_officers SET approved_at = UTC_TIMESTAMP() WHERE agricultural_officer_id = %s AND user_id = %s AND approved_at IS NULL", (91, 41)),
        ])
        self.connection.commit.assert_called_once_with()
        methods = [call[0] for call in self.connection.mock_calls]
        self.assertLess(max(index for index, name in enumerate(methods) if name == "cursor().execute"), methods.index("commit"))
        self.connection.rollback.assert_not_called()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_missing_user_and_farmer_accounts_cannot_be_approved(self):
        for user, code in ((None, "not_found"), (dict(self.user, role="farmer"), "not_officer")):
            with self.subTest(code=code):
                self.cursor.reset_mock()
                self.cursor.fetchone.side_effect = [user]
                result = approve_officer("officer@example.com")
                self.assertEqual(result["code"], code)
                self.assertEqual(self.cursor.execute.call_count, 1)
        self.connection.commit.assert_not_called()
        self.assertEqual(self.connection.rollback.call_count, 2)

    def test_active_disabled_missing_mismatched_and_inconsistent_profiles_are_refused(self):
        for user, profile, code in (
            (dict(self.user, status="active"), dict(self.profile, approved_at=datetime(2026, 1, 1)), "already_active"),
            (dict(self.user, status="disabled"), self.profile, "disabled"),
            (self.user, None, "inconsistent_account"),
            (self.user, dict(self.profile, user_id=999), "inconsistent_account"),
            (self.user, dict(self.profile, approved_at=datetime(2026, 1, 1)), "inconsistent_account"),
            (dict(self.user, status="active"), self.profile, "inconsistent_account"),
            (dict(self.user, status="unexpected"), self.profile, "inconsistent_account"),
        ):
            with self.subTest(user=user, profile=profile):
                self.connection.reset_mock()
                self.cursor.fetchone.side_effect = [user, profile]
                result = approve_officer("officer@example.com")
                self.assertEqual(result["code"], code)
                self.assertEqual(self.cursor.execute.call_count, 2)
                self.connection.commit.assert_not_called()
                self.connection.rollback.assert_called_once_with()
                self.cursor.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()

    def test_conditional_update_conflicts_roll_back_both_changes(self):
        for counts, executions in (([0], 3), ([1, 0], 4), ([2], 3), ([1, 2], 4)):
            with self.subTest(counts=counts):
                self.connection.reset_mock()
                self.cursor.fetchone.side_effect = [self.user, self.profile]
                # rowcount is read once after each conditional UPDATE.
                type(self.cursor).rowcount = unittest.mock.PropertyMock(side_effect=counts)
                result = approve_officer("officer@example.com")
                self.assertEqual(result["code"], "concurrent_change")
                self.assertEqual(self.cursor.execute.call_count, executions)
                self.connection.rollback.assert_called_once_with()
                self.connection.commit.assert_not_called()

    def test_failure_at_each_transaction_stage_rolls_back_and_closes(self):
        for stage in ("start_transaction", "cursor", "user_lookup", "profile_lookup", "user_update", "profile_update", "commit"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                self.cursor.fetchone.side_effect = [self.user, self.profile]
                if stage in ("user_lookup", "profile_lookup", "user_update", "profile_update"):
                    target = self.cursor.execute
                    target.side_effect = [None] * ["user_lookup", "profile_lookup", "user_update", "profile_update"].index(stage) + [Error("private SQL password")]
                else:
                    target = getattr(self.connection, stage)
                    target.side_effect = Error("private SQL password")
                with self.assertLogs("app.services.officer_approval_service", level="ERROR") as logs:
                    result = approve_officer("officer@example.com")
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("private", str(result) + str(logs.output))
                self.connection.rollback.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                if stage not in ("start_transaction", "cursor"):
                    self.cursor.close.assert_called_once_with()
                if stage != "commit":
                    self.connection.commit.assert_not_called()
                target.side_effect = None

    def test_connection_unavailable_and_no_connection_are_safe(self):
        for failure in (Error("private credentials"), None):
            with self.subTest(failure=failure):
                self.get_connection.side_effect = failure
                self.get_connection.return_value = None
                with self.assertLogs("app.services.officer_approval_service", level="ERROR"):
                    result = approve_officer("officer@example.com")
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("private", str(result))

    def test_rollback_and_cleanup_failures_do_not_expose_diagnostics(self):
        self.cursor.execute.side_effect = Error("private SQL")
        self.connection.rollback.side_effect = Error("private rollback")
        self.cursor.close.side_effect = Error("private cursor")
        self.connection.close.side_effect = Error("private connection")
        with self.assertLogs("app.services.officer_approval_service", level="WARNING") as logs:
            result = approve_officer("officer@example.com")
        self.assertFalse(result["ok"])
        self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.close.assert_called_once_with()

    def test_invalid_email_is_rejected_before_connection(self):
        for email in (None, "", "no-at", "a..b@example.com", "a@localhost", "a@example.com' OR 1=1"):
            self.assertEqual(approve_officer(email)["code"], "invalid_email")
        self.get_connection.assert_not_called()

    def test_two_competing_approvals_serialize_using_mocked_transaction_locks(self):
        # Simulate InnoDB locking reads, not a live database or a real DB race.
        row_lock = Lock()
        first_locked, second_attempted, permit_commit = Event(), Event(), Event()
        allocation_lock = Lock()
        connections = []
        state = {"active": False}

        def connect():
            connection = MagicMock()
            cursor = connection.cursor.return_value
            cursor.rowcount = 1
            with allocation_lock:
                index = len(connections)
                connections.append(connection)
            held = {"lock": False}

            def execute(sql, params):
                if sql.startswith("SELECT user_id"):
                    if index == 1:
                        second_attempted.set()
                    if not row_lock.acquire(timeout=5):
                        raise AssertionError("Mock transaction lock timed out")
                    held["lock"] = True
                    if index == 0:
                        first_locked.set()
                    cursor.fetchone.return_value = dict(self.user, status="active" if state["active"] else "pending")
                elif sql.startswith("SELECT agricultural_officer_id"):
                    cursor.fetchone.return_value = dict(self.profile, approved_at=datetime(2026, 1, 1) if state["active"] else None)

            def release():
                if held["lock"]:
                    held["lock"] = False
                    row_lock.release()

            def commit():
                if not permit_commit.wait(timeout=5):
                    raise AssertionError("Mock commit timed out")
                state["active"] = True
                release()

            cursor.execute.side_effect = execute
            connection.commit.side_effect = commit
            connection.rollback.side_effect = release
            connection.close.side_effect = release
            return connection

        self.get_connection.side_effect = connect
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(approve_officer, "officer@example.com")
                self.assertTrue(first_locked.wait(timeout=5))
                second = pool.submit(approve_officer, "officer@example.com")
                self.assertTrue(second_attempted.wait(timeout=5))
                permit_commit.set()
                self.assertTrue(first.result(timeout=5)["ok"])
                self.assertEqual(second.result(timeout=5)["code"], "already_active")
        finally:
            permit_commit.set()
        connections[0].commit.assert_called_once_with()
        connections[1].commit.assert_not_called()
        connections[1].rollback.assert_called_once_with()

    def test_cli_success_requires_confirmation_and_does_not_create_http_approval_route(self):
        result = self.runner.invoke(args=["approve-officer", "--email", " Officer@Example.COM "], input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("officer@example.com", result.output)
        self.assertIn("approved successfully", result.output)
        self.assertFalse(any("approv" in rule.rule for rule in self.app.url_map.iter_rules()))
        self.assertEqual(self.app.test_client().post("/officer/approve").status_code, 404)

    def test_cli_cancellation_missing_email_and_invalid_email_never_connect(self):
        for args, answer in ((["--email", "officer@example.com"], "n\n"), (["--email", "officer@example.com"], ""),
                             ([], "y\n"), (["--email", "invalid"], "y\n")):
            with self.subTest(args=args, answer=answer):
                result = self.runner.invoke(args=["approve-officer", *args], input=answer)
                self.assertNotEqual(result.exit_code, 0)
        self.get_connection.assert_not_called()

    def test_cli_refusals_are_nonzero_and_database_errors_are_sanitized(self):
        self.user["role"] = "farmer"
        result = self.runner.invoke(args=["approve-officer", "--email", "farmer@example.com"], input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Only officer", result.output)
        self.get_connection.side_effect = Error("private SQL password")
        with self.assertLogs("app.services.officer_approval_service", level="ERROR"):
            result = self.runner.invoke(args=["approve-officer", "--email", "officer@example.com"], input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not be confirmed", result.output)
        self.assertNotIn("private", result.output)


if __name__ == "__main__":
    unittest.main()
