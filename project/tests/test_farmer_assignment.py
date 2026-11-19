import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock
from unittest.mock import MagicMock, patch

import dotenv
from mysql.connector import Error

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.services.farmer_assignment_service import assign_farmer


class FarmerAssignmentTests(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live database forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.farmer_user = {"user_id": 41, "role": "farmer", "status": "active"}
        self.farmer = {"farmer_id": 91, "user_id": 41, "assigned_officer_id": None}
        self.officer_user = {"user_id": 52, "role": "officer", "status": "active"}
        self.officer = {"agricultural_officer_id": 102, "user_id": 52, "approved_at": datetime(2026, 1, 1)}
        self.rows = [self.farmer_user, self.farmer, self.officer_user, self.officer]
        self.cursor.fetchone.side_effect = lambda: self.rows[self.cursor.execute.call_count - 1]
        self.cursor.rowcount = 1
        helper = patch("app.services.farmer_assignment_service.get_db_connection", return_value=self.connection)
        self.helper = helper.start()
        self.addCleanup(helper.stop)
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-assignment-test-key")
        self.runner = self.app.test_cli_runner()

    def tearDown(self):
        self.guard.assert_not_called()

    def assign(self):
        return assign_farmer("0712 345 678", " Officer@Example.COM ")

    def test_success_normalizes_inputs_locks_accounts_and_stores_profile_foreign_key(self):
        result = self.assign()
        self.assertEqual(result["code"], "assigned")
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual([call.args for call in self.cursor.execute.call_args_list], [
            ("SELECT user_id, role, status FROM users WHERE phone_number = %s FOR UPDATE", ("+254712345678",)),
            ("SELECT farmer_id, user_id, assigned_officer_id FROM farmers WHERE user_id = %s FOR UPDATE", (41,)),
            ("SELECT user_id, role, status FROM users WHERE email = %s FOR UPDATE", ("officer@example.com",)),
            ("SELECT agricultural_officer_id, user_id, approved_at FROM agricultural_officers WHERE user_id = %s FOR UPDATE", (52,)),
            ("UPDATE farmers SET assigned_officer_id = %s WHERE farmer_id = %s AND user_id = %s AND assigned_officer_id IS NULL", (102, 91, 41)),
        ])
        # The officer's user ID (52) must never be stored as the foreign key.
        self.assertEqual(self.cursor.execute.call_args.args[1][0], self.officer["agricultural_officer_id"])
        self.assertNotEqual(self.cursor.execute.call_args.args[1][0], self.officer_user["user_id"])
        self.connection.start_transaction.assert_called_once_with()
        self.connection.cursor.assert_called_once_with(dictionary=True)
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_not_called()
        methods = [call[0] for call in self.connection.mock_calls]
        self.assertLess(max(i for i, name in enumerate(methods) if name == "cursor().execute"), methods.index("commit"))
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_supported_phone_and_email_formats_reuse_normalization(self):
        for phone in ("0712345678", "+254712345678", "254712345678", "00254712345678", "712345678", "(0712) 345-678"):
            self.connection.reset_mock()
            self.assertTrue(assign_farmer(phone, " OFFICER@EXAMPLE.COM ")["ok"])
            self.assertEqual(self.cursor.execute.call_args_list[0].args[1], ("+254712345678",))
            self.assertEqual(self.cursor.execute.call_args_list[2].args[1], ("officer@example.com",))

    def test_invalid_inputs_never_connect(self):
        for phone, email in ((None, "a@example.com"), ("0201234567", "a@example.com"),
                             ("0712345678' OR 1=1", "a@example.com"), ("0712345678", "invalid"),
                             ("0712345678", "a@example.com' OR 1=1"), ("0712345678", None)):
            self.assertEqual(assign_farmer(phone, email)["code"], "invalid_input")
        self.helper.assert_not_called()

    def test_missing_inactive_wrong_role_and_inconsistent_accounts_never_update(self):
        cases = [(0, None, "farmer_not_found"), (1, None, "inconsistent_farmer"),
                 (1, dict(self.farmer, user_id=999), "inconsistent_farmer"),
                 (1, dict(self.farmer, farmer_id=None), "inconsistent_farmer"),
                 (2, None, "officer_not_found"), (3, None, "invalid_officer_profile"),
                 (3, dict(self.officer, user_id=999), "invalid_officer_profile"),
                 (3, dict(self.officer, approved_at=None), "invalid_officer_profile"),
                 (3, dict(self.officer, agricultural_officer_id=None), "invalid_officer_profile")]
        for index, row, code in ((0, self.farmer_user, "invalid_farmer"), (2, self.officer_user, "invalid_officer")):
            cases.extend((index, dict(row, status=status), code) for status in ("pending", "disabled"))
            cases.append((index, dict(row, role="officer" if index == 0 else "farmer"), code))
        baseline = list(self.rows)
        for index, row, code in cases:
            with self.subTest(index=index, row=row):
                self.connection.reset_mock()
                self.rows = list(baseline)
                self.rows[index] = row
                self.assertEqual(self.assign()["code"], code)
                self.assertTrue(all(call.args[0].startswith("SELECT") for call in self.cursor.execute.call_args_list))
                self.connection.rollback.assert_called_once_with()
                self.connection.commit.assert_not_called()
                self.cursor.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()

    def test_same_officer_is_success_without_update_and_other_officer_is_refused(self):
        for assigned, ok, code in ((102, True, "already_assigned"), (999, False, "assignment_conflict")):
            with self.subTest(assigned=assigned):
                self.connection.reset_mock()
                self.farmer["assigned_officer_id"] = assigned
                result = self.assign()
                self.assertEqual((result["ok"], result["changed"], result["code"]), (ok, False, code))
                self.assertEqual(self.cursor.execute.call_count, 4)
                self.connection.rollback.assert_called_once_with()
                self.connection.commit.assert_not_called()

    def test_conditional_update_rowcount_conflict_is_rolled_back(self):
        for count in (0, 2):
            self.connection.reset_mock()
            self.cursor.rowcount = count
            result = self.assign()
            self.assertEqual(result["code"], "concurrent_change")
            self.connection.rollback.assert_called_once_with()
            self.connection.commit.assert_not_called()

    def test_failures_at_each_transaction_stage_rollback_and_close_safely(self):
        for stage in ("start_transaction", "cursor", 0, 1, 2, 3, 4, "commit"):
            with self.subTest(stage=stage):
                self.connection.reset_mock()
                target = self.cursor.execute if isinstance(stage, int) else getattr(self.connection, stage)
                target.side_effect = [None] * stage + [Error("private SQL password")] if isinstance(stage, int) else Error("private SQL password")
                with self.assertLogs("app.services.farmer_assignment_service", level="ERROR") as logs:
                    result = self.assign()
                self.assertEqual(result["code"], "database_error")
                self.assertNotIn("private", str(result) + str(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))
                self.connection.rollback.assert_called_once_with()
                self.connection.close.assert_called_once_with()
                if stage not in ("start_transaction", "cursor"):
                    self.cursor.close.assert_called_once_with()
                if stage != "commit":
                    self.connection.commit.assert_not_called()
                target.side_effect = None

    def test_connection_failures_and_lock_errors_are_not_retried(self):
        self.helper.side_effect = Error("private credentials")
        with self.assertLogs("app.services.farmer_assignment_service", level="ERROR"):
            self.assertEqual(self.assign()["code"], "database_error")
        self.helper.assert_called_once_with()
        self.helper.side_effect = None
        for errno in (1205, 1213):
            self.connection.reset_mock()
            self.cursor.execute.side_effect = Error("private lock diagnostic", errno=errno)
            with self.assertLogs("app.services.farmer_assignment_service", level="ERROR"):
                self.assertEqual(self.assign()["code"], "database_error")
            self.cursor.execute.assert_called_once()
            self.connection.rollback.assert_called_once_with()

    def test_rollback_and_cleanup_failures_are_sanitized(self):
        self.cursor.execute.side_effect = Error("private")
        self.connection.rollback.side_effect = Error("private")
        self.cursor.close.side_effect = Error("private")
        self.connection.close.side_effect = Error("private")
        with self.assertLogs("app.services.farmer_assignment_service", level="WARNING") as logs:
            result = self.assign()
        self.assertEqual(result["code"], "database_error")
        self.assertNotIn("private", str(result) + str(logs.output))
        self.connection.close.assert_called_once_with()

    def test_cli_confirmation_displays_normalized_contacts_and_assignment_succeeds(self):
        result = self.runner.invoke(args=["assign-farmer", "--farmer-phone", "0712 345 678", "--officer-email", " Officer@Example.COM "], input="y\n")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("+254712345678", result.output)
        self.assertIn("officer@example.com", result.output)
        self.assertIn("assigned successfully", result.output)
        self.assertFalse(any("assign" in rule.rule for rule in self.app.url_map.iter_rules()))

    def test_cli_cancellation_invalid_or_missing_options_do_not_connect(self):
        valid = ["assign-farmer", "--farmer-phone", "0712345678", "--officer-email", "officer@example.com"]
        for args, answer in ((valid, "n\n"), (valid, ""), (["assign-farmer"], "y\n"),
                             (valid[:-1] + ["invalid"], "y\n"),
                             (["assign-farmer", "--farmer-phone", "invalid", "--officer-email", "officer@example.com"], "y\n")):
            self.assertNotEqual(self.runner.invoke(args=args, input=answer).exit_code, 0)
        self.helper.assert_not_called()

    def test_cli_noop_is_success_conflict_is_failure_and_database_errors_are_safe(self):
        args = ["assign-farmer", "--farmer-phone", "0712345678", "--officer-email", "officer@example.com"]
        self.farmer["assigned_officer_id"] = 102
        result = self.runner.invoke(args=args, input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("no change is necessary", result.output)
        self.farmer["assigned_officer_id"] = 999
        self.connection.reset_mock()
        result = self.runner.invoke(args=args, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Reassignment is not permitted", result.output)
        self.helper.side_effect = Error("private SQL password")
        with self.assertLogs("app.services.farmer_assignment_service", level="ERROR"):
            result = self.runner.invoke(args=args, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("could not be confirmed", result.output)
        self.assertNotIn("private", result.output)

    def test_competing_assignments_serialize_and_never_overwrite_existing_relationship(self):
        # Model InnoDB locking reads with mocked connections, not real records.
        for second_email, expected in (("officer@example.com", "already_assigned"), ("other@example.com", "assignment_conflict")):
            with self.subTest(second_email=second_email):
                row_lock, allocation_lock = Lock(), Lock()
                first_locked, second_attempted, permit_commit = Event(), Event(), Event()
                connections = []
                state = {"assigned": None}

                def connect():
                    connection = MagicMock()
                    cursor = connection.cursor.return_value
                    cursor.rowcount = 1
                    with allocation_lock:
                        index = len(connections)
                        connections.append(connection)
                    tx = {"held": False, "officer_id": None, "new_id": None}

                    def execute(sql, params):
                        if "WHERE phone_number" in sql:
                            if index == 1:
                                second_attempted.set()
                            if not row_lock.acquire(timeout=5):
                                raise AssertionError("Mock row lock timed out")
                            tx["held"] = True
                            first_locked.set()
                            cursor.fetchone.return_value = dict(self.farmer_user)
                        elif sql.startswith("SELECT farmer_id"):
                            cursor.fetchone.return_value = dict(self.farmer, assigned_officer_id=state["assigned"])
                        elif "WHERE email" in sql:
                            tx["officer_id"] = 102 if params[0] == "officer@example.com" else 103
                            tx["officer_user_id"] = 52 if params[0] == "officer@example.com" else 53
                            cursor.fetchone.return_value = dict(self.officer_user, user_id=tx["officer_user_id"])
                        elif sql.startswith("SELECT agricultural_officer_id"):
                            cursor.fetchone.return_value = dict(self.officer, agricultural_officer_id=tx["officer_id"], user_id=tx["officer_user_id"])
                        elif sql.startswith("UPDATE"):
                            tx["new_id"] = params[0]

                    def release():
                        if tx["held"]:
                            tx["held"] = False
                            row_lock.release()

                    def commit():
                        if not permit_commit.wait(timeout=5):
                            raise AssertionError("Mock commit timed out")
                        state["assigned"] = tx["new_id"]
                        release()

                    cursor.execute.side_effect = execute
                    connection.commit.side_effect = commit
                    connection.rollback.side_effect = release
                    connection.close.side_effect = release
                    return connection

                self.helper.side_effect = connect
                with ThreadPoolExecutor(max_workers=2) as pool:
                    try:
                        first = pool.submit(assign_farmer, "0712345678", "officer@example.com")
                        self.assertTrue(first_locked.wait(timeout=5))
                        second = pool.submit(assign_farmer, "0712345678", second_email)
                        self.assertTrue(second_attempted.wait(timeout=5))
                        permit_commit.set()
                        self.assertEqual(first.result(timeout=5)["code"], "assigned")
                        self.assertEqual(second.result(timeout=5)["code"], expected)
                    finally:
                        permit_commit.set()
                self.assertEqual(state["assigned"], 102)
                connections[0].commit.assert_called_once_with()
                connections[1].commit.assert_not_called()
                self.assertTrue(all(call.args[0].startswith("SELECT") for call in connections[1].cursor.return_value.execute.call_args_list))


if __name__ == "__main__":
    unittest.main()
