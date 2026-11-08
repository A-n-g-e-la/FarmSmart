"""Synthetic connector tests only: no services, sockets or live SQL."""

import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import MagicMock, call, patch

import dotenv
from mysql.connector.errors import Error, ReadTimeoutError, WriteTimeoutError

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.db import UssdDatabaseError, get_db_connection, get_ussd_db_connection
    from app.models.ussd_repository import UssdPersistenceError, ussd_transaction


DEFAULTS = {
    "USSD_DB_CONNECTION_TIMEOUT": 3,
    "USSD_DB_READ_TIMEOUT": 5,
    "USSD_DB_WRITE_TIMEOUT": 5,
    "USSD_DB_LOCK_WAIT_TIMEOUT": 2,
    "USSD_DB_STATEMENT_TIMEOUT": 3,
}
PRIVATE_DETAIL = "synthetic-private-connection-detail"


class UssdPerformanceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.connection, self.initializer, self.cursor = MagicMock(), MagicMock(), MagicMock()
        self.connection.cursor.side_effect = lambda **kwargs: self.cursor if kwargs.get("dictionary") else self.initializer
        self.cursor.fetchone.return_value = None
        guard = patch("mysql.connector.connect", return_value=self.connection)
        self.connector = guard.start()
        self.addCleanup(guard.stop)
        self.app = create_app()
        self.app.config.update(DEFAULTS)
        self.app.config.update(TESTING=True, USSD_PERSISTENT_FLOW_ENABLED=False,
                               USSD_RATE_LIMITING_ENABLED=False,
                               MYSQL_HOST="localhost", MYSQL_PORT=3306,
                               MYSQL_DATABASE="synthetic_not_connected",
                               MYSQL_USER="synthetic", MYSQL_PASSWORD="synthetic")
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)

    def test_default_connector_arguments_and_session_initialization(self):
        self.assertIs(get_ussd_db_connection(), self.connection)
        self.connector.assert_called_once_with(
            host="localhost", port=3306, database="synthetic_not_connected",
            user="synthetic", password="synthetic",
            connection_timeout=3, read_timeout=5, write_timeout=5,
        )
        self.initializer.execute.assert_has_calls([
            call("SET SESSION innodb_lock_wait_timeout = %s", (2,)),
            call("SET SESSION max_statement_time = %s", (3,)),
        ])
        self.assertEqual(self.initializer.execute.call_count, 2)
        self.initializer.close.assert_called_once_with()
        self.connection.start_transaction.assert_not_called()
        self.connection.commit.assert_not_called()
        self.connection.close.assert_not_called()

    def test_all_timeout_values_are_validated_before_connecting(self):
        for setting in DEFAULTS:
            for value in (None, 0, -1, 31, True, False, 1.5, "0", "-1", "31", "1.5", " 3", "3 ", "03", ""):
                with self.subTest(setting=setting, value=value):
                    self.app.config.update(DEFAULTS)
                    self.app.config[setting] = value
                    with self.assertRaisesRegex(UssdDatabaseError, "^USSD database connection is unavailable\\.$"):
                        get_ussd_db_connection()
                    self.connector.assert_not_called()

    def test_integer_and_environment_string_boundaries(self):
        for value in (1, "1", 30, "30"):
            with self.subTest(value=value):
                self.app.config.update({name: value for name in DEFAULTS})
                self.assertIs(get_ussd_db_connection(), self.connection)
                arguments = self.connector.call_args.kwargs
                for name in ("connection_timeout", "read_timeout", "write_timeout"):
                    self.assertEqual(arguments[name], int(value))
                    self.assertIs(type(arguments[name]), int)
                self.assertEqual(self.initializer.execute.call_args.args[1], (int(value),))

    def test_missing_configuration_fails_before_connecting(self):
        del self.app.config["USSD_DB_STATEMENT_TIMEOUT"]
        with self.assertRaises(UssdDatabaseError):
            get_ussd_db_connection()
        self.connector.assert_not_called()

    def test_session_limits_precede_transaction_and_repository_queries(self):
        events = []
        self.initializer.execute.side_effect = lambda sql, params: events.append(sql)
        self.initializer.close.side_effect = lambda: events.append("initializer closed")
        self.connection.start_transaction.side_effect = lambda: events.append("transaction started")
        self.cursor.execute.side_effect = lambda sql, params: events.append("repository query")
        with ussd_transaction() as store:
            self.assertIsNone(store.get_credentials(91))
        self.assertEqual(events, ["SET SESSION innodb_lock_wait_timeout = %s",
                                  "SET SESSION max_statement_time = %s", "initializer closed",
                                  "transaction started", "repository query"])
        self.connection.commit.assert_called_once_with()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_connection_timeout_is_safe_and_not_retried(self):
        self.connector.side_effect = Error(PRIVATE_DETAIL, errno=2003)
        with self.assertRaisesRegex(UssdDatabaseError, "^USSD database connection is unavailable\\.$"):
            get_ussd_db_connection()
        self.connector.assert_called_once()
        self.connection.start_transaction.assert_not_called()

    def test_each_initialization_statement_failure_aborts_before_transaction(self):
        for failing_statement in (0, 1):
            with self.subTest(statement=failing_statement):
                self.connection.reset_mock()
                self.initializer.reset_mock()
                self.initializer.execute.side_effect = ([Error(PRIVATE_DETAIL)] if failing_statement == 0
                                                         else [None, Error(PRIVATE_DETAIL)])
                with self.assertRaises(UssdPersistenceError) as error:
                    with ussd_transaction():
                        self.fail("Failed initialization must not yield a repository")
                self.assertNotIn(PRIVATE_DETAIL, str(error.exception))
                self.connection.start_transaction.assert_not_called()
                self.connection.commit.assert_not_called()
                self.connection.rollback.assert_called_once_with()
                self.initializer.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()

    def test_initialization_cursor_creation_failure_closes_connection(self):
        self.connection.cursor.side_effect = ValueError(PRIVATE_DETAIL)
        with self.assertRaises(UssdDatabaseError):
            get_ussd_db_connection()
        self.connection.rollback.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        self.connection.start_transaction.assert_not_called()

    def test_initialization_cursor_close_failure_never_returns_connection(self):
        self.initializer.close.side_effect = [Error(PRIVATE_DETAIL), None]
        with self.assertRaises(UssdDatabaseError):
            get_ussd_db_connection()
        self.connection.start_transaction.assert_not_called()
        self.connection.rollback.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_initialization_cleanup_failures_do_not_expose_details_or_skip_close(self):
        self.initializer.execute.side_effect = ValueError(PRIVATE_DETAIL)
        self.initializer.close.side_effect = Error(PRIVATE_DETAIL)
        self.connection.rollback.side_effect = Error(PRIVATE_DETAIL)
        self.connection.close.side_effect = Error(PRIVATE_DETAIL)
        with self.assertLogs("app.db", level="WARNING") as logs:
            with self.assertRaises(UssdDatabaseError) as error:
                get_ussd_db_connection()
        self.assertNotIn(PRIVATE_DETAIL, str(error.exception))
        self.assertNotIn(PRIVATE_DETAIL, str(logs.output))
        self.connection.close.assert_called_once_with()

    def test_read_write_statement_and_lock_timeouts_roll_back_repository_unit(self):
        for failure in (ReadTimeoutError(PRIVATE_DETAIL), WriteTimeoutError(PRIVATE_DETAIL),
                        Error(PRIVATE_DETAIL, errno=1969), Error(PRIVATE_DETAIL, errno=1205)):
            with self.subTest(error_type=type(failure).__name__, errno=failure.errno):
                self.connection.reset_mock()
                self.cursor.reset_mock()
                self.cursor.execute.side_effect = failure
                with self.assertRaises(UssdPersistenceError) as error:
                    with ussd_transaction() as store:
                        store.get_credentials(91)
                self.assertNotIn(PRIVATE_DETAIL, str(error.exception))
                self.connection.commit.assert_not_called()
                self.connection.rollback.assert_called_once_with()
                self.cursor.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()
        self.assertEqual(self.connector.call_count, 4)  # One connection, no retries per unit.

    def test_transaction_start_failure_is_sanitized_and_closed(self):
        self.connection.start_transaction.side_effect = ReadTimeoutError(PRIVATE_DETAIL)
        with self.assertRaises(UssdPersistenceError):
            with ussd_transaction():
                self.fail("Transaction must not start")
        self.connection.rollback.assert_called_once_with()
        self.connection.close.assert_called_once_with()
        self.connection.commit.assert_not_called()

    def test_commit_acknowledgement_timeout_is_uncertain_and_never_retried(self):
        server_state = {"committed": False}
        def commit_then_lose_acknowledgement():
            server_state["committed"] = True
            raise ReadTimeoutError(PRIVATE_DETAIL)
        self.connection.commit.side_effect = commit_then_lose_acknowledgement
        with self.assertRaisesRegex(UssdPersistenceError, "^USSD transaction could not be confirmed\\.$"):
            with ussd_transaction() as store:
                store.get_credentials(91)
        self.assertTrue(server_state["committed"])  # A later rollback cannot undo a completed commit.
        self.connector.assert_called_once()
        self.connection.commit.assert_called_once_with()
        self.connection.rollback.assert_called_once_with()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_called_once_with()

    def test_general_web_helper_keeps_existing_connection_arguments(self):
        self.app.config["USSD_DB_CONNECTION_TIMEOUT"] = "invalid"
        self.assertIs(get_db_connection(), self.connection)
        self.connector.assert_called_once_with(host="localhost", port=3306, database="synthetic_not_connected",
                                               user="synthetic", password="synthetic")
        self.connection.cursor.assert_not_called()

    def test_legacy_callback_needs_no_database_even_with_bad_timeout_configuration(self):
        self.connector.side_effect = AssertionError("Database forbidden")
        self.app.config.update({name: "invalid" for name in DEFAULTS})
        response = self.app.test_client().post("/ussd", data={
            "sessionId": "synthetic", "serviceCode": "*384*123#", "phoneNumber": "+254700000000", "text": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_data(as_text=True).startswith("CON Welcome to FarmAdvis"))
        self.connector.assert_not_called()

    def test_safe_menu_and_pin_management_failures_with_connection_timeout(self):
        from app.services.ussd_authentication_service import authenticate_ussd_pin
        from app.services.farmer_ussd_pin_service import save_farmer_ussd_pin
        from app.models.ussd_repository import SAFE_RESPONSES
        import base64
        self.app.config.update(USSD_SERVICE_CODE="*384*123#",
                               USSD_METADATA_HMAC_KEY=base64.b64encode(b"m" * 32).decode())
        self.connector.side_effect = ReadTimeoutError(PRIVATE_DETAIL)
        with patch("app.services.ussd_authentication_service.verify_ussd_pin") as verifier, \
                patch("app.services.farmer_ussd_pin_service.hash_ussd_pin") as hasher:
            result = authenticate_ussd_pin("+254700000000", "synthetic", "*384*123#", "1*0042")
            self.assertFalse(result["authenticated"])
            self.assertEqual(result["response"], SAFE_RESPONSES["verification_failed"])
            saved = save_farmer_ussd_pin(41, 91, "synthetic-password", "0042", "0042")
            self.assertFalse(saved["ok"])
            self.assertEqual(saved["code"], "unavailable")
            self.assertNotIn(PRIVATE_DETAIL, str((result, saved)))
            verifier.assert_not_called()
            hasher.assert_not_called()

    def test_config_defaults_and_environment_overrides(self):
        config_path = Path(__file__).resolve().parents[1] / "app" / "config" / "__init__.py"
        for environment in ({}, {name: "7" for name in DEFAULTS}):
            with patch.dict(os.environ, environment, clear=True), patch.object(dotenv, "load_dotenv", return_value=False):
                config = runpy.run_path(str(config_path))["Config"]
            for name, default in DEFAULTS.items():
                self.assertEqual(getattr(config, name), environment.get(name, default))
            self.assertIs(config.USSD_PERSISTENT_FLOW_ENABLED, False)


if __name__ == "__main__":
    unittest.main()
