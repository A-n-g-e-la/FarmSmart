"""Offline guard checks. No test in this file opens a database connection."""
import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

DIRECTORY = Path(__file__).resolve().parent / "integration"
spec = importlib.util.spec_from_file_location("farmadvis_mariadb_runner", DIRECTORY / "mariadb_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


def environment():
    return dict(RUN_MARIADB_INTEGRATION_TESTS="1", TEST_MYSQL_HOST="localhost", TEST_MYSQL_PORT="3306",
                TEST_MYSQL_DATABASE="farmadvis_integration_20261107_abcdef12",
                TEST_MYSQL_APPROVED_DATABASE="farmadvis_integration_20261107_abcdef12",
                TEST_MYSQL_USER="farmadvis_it_abcd", TEST_MYSQL_PASSWORD="synthetic-database-password")


class MariaDbIntegrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.settings = runner.Settings.from_environment(environment())
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.identity = dict(db=self.settings.database, version="10.4.28-MariaDB", port=3306,
                             mode=runner.MODE, isolation="REPEATABLE-READ", fk=1, checks=1,
                             account=self.settings.user + "@localhost", role=None)
        self.global_grants = []
        self.schema_grants = [dict(TABLE_SCHEMA=self.settings.database.replace("_", "\\_"), PRIVILEGE_TYPE=privilege, IS_GRANTABLE="NO")
                              for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")]
        self.extra_grants = 0
        def execute(sql, params=()):
            if sql.startswith("SELECT DATABASE()"):
                self.cursor.fetchone.return_value = self.identity
            elif "USER_PRIVILEGES" in sql:
                self.cursor.fetchall.return_value = self.global_grants
            elif "SCHEMA_PRIVILEGES" in sql:
                self.cursor.fetchall.return_value = self.schema_grants
            elif "COUNT(*)" in sql:
                self.cursor.fetchone.return_value = {"n": self.extra_grants}
        self.cursor.execute.side_effect = execute
        self.raw = MagicMock(return_value=self.connection)
        self.resolver = MagicMock(return_value=[(2, 1, 6, "", ("127.0.0.1", 3306))])
        self.guard = runner.ConnectionGuard(self.settings, self.raw, self.resolver)

    def test_missing_opt_in_and_dedicated_settings_never_connect(self):
        for key in environment():
            env = environment()
            env.pop(key)
            with self.subTest(setting=key), self.assertRaises(runner.IntegrationSafetyError):
                runner.Settings.from_environment(env)
        self.raw.assert_not_called()

    def test_malformed_opt_in_hosts_ports_names_and_accounts_are_rejected(self):
        invalid = {
            "RUN_MARIADB_INTEGRATION_TESTS": ["true", "0", " 1", ""],
            "TEST_MYSQL_HOST": ["remote.example", "0.0.0.0", "::1", "localhost.example"],
            "TEST_MYSQL_PORT": ["3307", "03306", "3306 ", ""],
            "TEST_MYSQL_USER": ["root", "administrator", "farmadvis_it_x;"],
            "TEST_MYSQL_DATABASE": ["farmsmart", "farmsmart_test", "mysql", "farmadvis_migration_rehearsal_20261106",
                                    "farmadvis_restore_verify_20261106_abcd", "farmadvis_integration_20261107_abc;DROP"],
        }
        for key, values in invalid.items():
            for value in values:
                env = dict(environment(), **{key: value})
                if key == "TEST_MYSQL_DATABASE":
                    env["TEST_MYSQL_APPROVED_DATABASE"] = value
                with self.subTest(setting=key), self.assertRaises(runner.IntegrationSafetyError):
                    runner.Settings.from_environment(env)
        self.raw.assert_not_called()

    def test_approved_name_must_match_and_development_values_are_not_fallbacks(self):
        env = environment()
        env["TEST_MYSQL_APPROVED_DATABASE"] += "1"
        with self.assertRaises(runner.IntegrationSafetyError):
            runner.Settings.from_environment(env)
        with self.assertRaises(runner.IntegrationSafetyError):
            runner.Settings.from_environment(dict(RUN_MARIADB_INTEGRATION_TESTS="1", MYSQL_DATABASE="farmsmart"))
        with self.assertRaises(runner.IntegrationSafetyError):
            runner.Settings("localhost", 3306, "farmsmart", self.settings.user, self.settings.password)

    def test_guard_rejects_overrides_aliases_and_positional_arguments_before_connect(self):
        valid = self.settings.connection_arguments()
        for changes in ({"database": "farmsmart"}, {"host": "remote.example"}, {"port": "3306"},
                        {"user": "root"}, {"password": "different"}, {"option_files": "private"}, {"db": "farmsmart"}):
            with self.assertRaises(runner.IntegrationSafetyError):
                self.guard(**dict(valid, **changes))
        with self.assertRaises(runner.IntegrationSafetyError):
            self.guard(valid)
        self.raw.assert_not_called()

    def test_remote_dns_resolution_is_rejected_before_connect(self):
        self.resolver.return_value = [(2, 1, 6, "", ("192.0.2.1", 3306))]
        with self.assertRaises(runner.IntegrationSafetyError):
            self.guard(**self.settings.connection_arguments())
        self.raw.assert_not_called()

    def test_valid_connection_checks_identity_privileges_and_bounds_resources(self):
        connection = self.guard(**self.settings.connection_arguments())
        self.assertIs(connection, self.connection)
        kwargs = self.raw.call_args.kwargs
        self.assertEqual((kwargs["connection_timeout"], kwargs["read_timeout"], kwargs["write_timeout"]), (5, 10, 10))
        self.assertEqual(kwargs["collation"], "utf8mb4_unicode_ci")
        self.connection.rollback.assert_called_once_with()
        self.cursor.close.assert_called_once_with()
        self.connection.close.assert_not_called()
        self.assertFalse(self.guard.failed)
        self.assertTrue(all(call.args[0].startswith("SELECT ") for call in self.cursor.execute.call_args_list))

    def test_wrong_post_connection_identity_or_enforcement_closes_resources(self):
        for field, value in (("db", "farmsmart"), ("version", "10.5.0-MariaDB"), ("port", 3307),
                             ("mode", "STRICT_TRANS_TABLES"), ("fk", 0), ("checks", 0),
                             ("role", "powerful_role"), ("account", "root@localhost")):
            original = self.identity[field]
            self.identity[field] = value
            self.connection.reset_mock()
            with self.subTest(field=field), self.assertRaises(runner.IntegrationSafetyError):
                self.guard(**self.settings.connection_arguments())
            self.connection.close.assert_called_once_with()
            self.identity[field] = original

    def test_global_other_database_extra_and_grantable_privileges_fail_closed(self):
        self.global_grants = [dict(PRIVILEGE_TYPE="SELECT", IS_GRANTABLE="NO")]
        with self.assertRaises(runner.IntegrationSafetyError):
            self.guard(**self.settings.connection_arguments())
        self.global_grants = []
        for field, value in (("TABLE_SCHEMA", "farmsmart"), ("IS_GRANTABLE", "YES"), ("PRIVILEGE_TYPE", "CREATE")):
            original = self.schema_grants[0][field]
            self.schema_grants[0][field] = value
            with self.assertRaises(runner.IntegrationSafetyError):
                self.guard(**self.settings.connection_arguments())
            self.schema_grants[0][field] = original
        self.extra_grants = 1
        with self.assertRaises(runner.IntegrationSafetyError):
            self.guard(**self.settings.connection_arguments())

    def test_escaped_schema_grants_are_compared_to_the_exact_database(self):
        for row in self.schema_grants:
            row["TABLE_SCHEMA"] = self.settings.database.replace("_", "\\_")
        self.assertIs(self.guard(**self.settings.connection_arguments()), self.connection)

    def test_unescaped_wildcard_schema_grants_are_rejected(self):
        for row in self.schema_grants:
            row["TABLE_SCHEMA"] = self.settings.database
        with self.assertRaises(runner.IntegrationSafetyError):
            self.guard(**self.settings.connection_arguments())
        self.connection.close.assert_called_once_with()

    def test_connector_and_cursor_errors_are_sanitized_and_closed(self):
        for fail in (self.raw, self.cursor.execute):
            self.connection.reset_mock()
            fail.side_effect = ValueError("private configuration " + self.settings.password)
            with self.assertRaises(runner.IntegrationSafetyError) as caught:
                self.guard(**self.settings.connection_arguments())
            self.assertEqual(str(caught.exception), "MariaDB integration connection refused.")
            self.assertTrue(self.guard.failed)
            if fail is self.cursor.execute:
                self.cursor.close.assert_called_once_with()
                self.connection.close.assert_called_once_with()
            fail.side_effect = None

    def test_test_session_timezone_is_scoped_and_never_global(self):
        runtime = runner.Runtime(self.settings, self.raw, self.guard, None)
        with runtime.timezone("+03:00"):
            self.guard(**self.settings.connection_arguments())
        self.assertIsNone(self.guard.local.zone)
        calls = [call for call in self.cursor.execute.call_args_list if call.args[0].startswith("SET ")]
        self.assertEqual([(call.args[0], call.args[1]) for call in calls], [("SET SESSION time_zone=%s", ("+03:00",))])
        with self.assertRaises(runner.IntegrationSafetyError):
            with runtime.timezone("SYSTEM"):
                pass

    def test_ephemeral_config_uses_independent_keys_and_keeps_ussd_disabled(self):
        config = runner.synthetic_config(self.settings)
        pepper = base64.b64decode(json.loads(config["USSD_PIN_PEPPERS"])["1"])
        metadata = base64.b64decode(config["USSD_METADATA_HMAC_KEY"])
        self.assertEqual(len(pepper), 32)
        self.assertEqual(len(metadata), 32)
        self.assertTrue(pepper != metadata and pepper != config["SECRET_KEY"].encode() and metadata != config["SECRET_KEY"].encode())
        self.assertFalse(config["USSD_PERSISTENT_FLOW_ENABLED"])
        self.assertTrue(config["TESTING"])
        self.assertFalse(config["DEBUG"])
        self.assertNotIn(self.settings.password, repr(self.settings))

    def test_disabled_runner_exits_without_connecting_or_printing_secrets(self):
        with patch.dict("os.environ", {}, clear=True), patch("mysql.connector.connect") as connect, patch("sys.stderr", new_callable=io.StringIO) as output:
            self.assertEqual(runner.main(), 2)
            connect.assert_not_called()
            self.assertNotIn(self.settings.password, output.getvalue())

    def test_cached_application_import_is_refused(self):
        with patch.dict(sys.modules, {"app": object()}), self.assertRaises(runner.IntegrationSafetyError):
            with runner.isolated_runtime(self.settings):
                self.fail("Cached application imports must be refused.")

    def test_fresh_import_blocks_dotenv_and_network_before_application_imports(self):
        code = '''
import os,sys
from unittest.mock import patch
sys.path.insert(0,'tests/integration')
import mariadb_runner as runner
settings=runner.Settings('localhost',3306,'farmadvis_integration_20261107_abcdef12','farmadvis_it_abcd','synthetic')
os.environ.update(MYSQL_DATABASE='farmsmart',MYSQL_HOST='remote.invalid',SECRET_KEY='development-secret')
with patch('mysql.connector.connect',side_effect=AssertionError('Database forbidden')) as raw, patch('dotenv.load_dotenv',side_effect=AssertionError('dotenv forbidden')):
 with runner.isolated_runtime(settings) as runtime:
  assert runtime.app.config['MYSQL_DATABASE']==settings.database
  assert runtime.app.config['MYSQL_HOST']=='localhost'
  assert runtime.app.config['SECRET_KEY']!='development-secret'
  assert runtime.app.config['USSD_PERSISTENT_FLOW_ENABLED'] is False
  from app.db import get_db_connection
  with patch.object(runtime.guard,'connector',side_effect=RuntimeError('Synthetic connection refusal')) as blocked:
   with runtime.app.app_context():
    try:
     get_db_connection()
    except runner.IntegrationSafetyError:
     pass
    else:
     raise AssertionError('Application connection bypassed guard')
   assert blocked.call_args.kwargs['database']==settings.database
   blocked.assert_called_once()
 raw.assert_not_called()
'''
        result = subprocess.run([sys.executable, "-B", "-c", code], cwd=DIRECTORY.parents[1], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, "Fresh-process isolation check failed; details suppressed.")

    def test_failure_reporting_suppresses_exception_values_and_tracebacks(self):
        class Failure(unittest.TestCase):
            def runTest(inner):
                raise ValueError("synthetic-secret-do-not-print")
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, resultclass=runner.SafeResult).run(Failure())
        self.assertFalse(result.wasSuccessful())
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_integration_directory_is_excluded_from_default_test_discovery(self):
        self.assertFalse((DIRECTORY / "__init__.py").exists())
        self.assertEqual(list(DIRECTORY.glob("test*.py")), [])

    def test_scenario_import_and_collection_do_not_connect_or_run_fixtures(self):
        scenario_spec = importlib.util.spec_from_file_location("farmadvis_mariadb_scenarios", DIRECTORY / "scenarios.py")
        scenarios = importlib.util.module_from_spec(scenario_spec)
        with patch("dotenv.load_dotenv", return_value=False), patch("mysql.connector.connect", side_effect=AssertionError("Database forbidden")) as connect:
            scenario_spec.loader.exec_module(scenarios)
            suite = scenarios.build_suite(None)
            self.assertEqual(suite.countTestCases(), 20)
            self.assertEqual(len({case.id() for case in suite}), 20)
            connect.assert_not_called()

    def schema_fixture(self):
        """Complete fake metadata; individual regressions mutate real requirements."""
        expected = runner.approved_schema(self.settings.database)
        metadata = {name: [] for name in ("COLUMNS", "STATISTICS", "KEY_COLUMN_USAGE", "CHECK_CONSTRAINTS")}
        source = (DIRECTORY.parents[1] / "schema" / "schema.sql").read_text(encoding="utf-8")
        clauses = {}
        for table, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\) ENGINE=", source, re.S):
            for clause in runner._schema_clauses(re.sub(r"--[^\n]*", "", body)):
                match = re.fullmatch(r"CONSTRAINT (\w+) CHECK \((.*)\)", clause, re.S)
                if match:
                    clauses[(table, match[1])] = match[2]
        for key, value in expected.items():
            kind, table, name, *position = key
            if kind == "column":
                metadata["COLUMNS"].append(dict(zip(
                    ("TABLE_NAME", "COLUMN_NAME", "COLUMN_TYPE", "IS_NULLABLE", "COLUMN_DEFAULT", "EXTRA", "COLLATION_NAME"),
                    (table, name, *value))))
            elif kind == "index":
                metadata["STATISTICS"].append(dict(zip(
                    ("TABLE_NAME", "INDEX_NAME", "SEQ_IN_INDEX", "COLUMN_NAME", "NON_UNIQUE", "SUB_PART", "INDEX_TYPE"),
                    (table, name, position[0], *value))))
            elif kind == "foreign":
                metadata["KEY_COLUMN_USAGE"].append(dict(zip(
                    ("TABLE_NAME", "CONSTRAINT_NAME", "ORDINAL_POSITION", "COLUMN_NAME", "REFERENCED_TABLE_SCHEMA", "REFERENCED_TABLE_NAME", "REFERENCED_COLUMN_NAME", "DELETE_RULE", "UPDATE_RULE"),
                    (table, name, position[0], *value))))
            else:
                metadata["CHECK_CONSTRAINTS"].append(dict(TABLE_NAME=table, CONSTRAINT_NAME=name, CHECK_CLAUSE=clauses[(table, name)]))
        runtime = runner.Runtime(self.settings, self.guard, self.guard, None)
        def sql(statement, parameters=()):
            if "information_schema.TABLES" in statement:
                return {"rows": [dict(TABLE_NAME=table, ENGINE="InnoDB") for table in runner.TABLES]}
            for name, rows in metadata.items():
                if "information_schema." + name in statement:
                    return {"rows": rows}
            return {"rows": [{"n": 0}]}
        runtime.sql = MagicMock(side_effect=sql)
        return runtime, metadata

    def test_approved_schema_contract_contains_required_structures(self):
        contract = runner.approved_schema(self.settings.database)
        self.assertEqual({kind: sum(key[0] == kind for key in contract) for kind in ("column", "index", "foreign", "check")},
                         dict(column=58, index=18, foreign=7, check=12))
        self.assertEqual(contract[("column", "farm_plots", "area_acres")], ("decimal(12,6)", "NO", None, "", None))
        self.assertEqual(contract[("column", "farmer_ussd_credentials", "pin_hash")], ("varchar(255)", "YES", None, "", "ascii_bin"))
        self.assertEqual(contract[("index", "users", "uq_users_phone_number", 1)], ("phone_number", 0, None, "BTREE"))
        self.assertEqual(contract[("foreign", "ussd_callback_results", "fk_ussd_callback_session", 1)],
                         ("session_key", self.settings.database, "ussd_sessions", "session_key", "CASCADE", "RESTRICT"))

    def test_matching_mariadb_schema_metadata_is_accepted_without_writes(self):
        runtime, metadata = self.schema_fixture()
        for row in metadata["COLUMNS"]:
            if row["COLUMN_TYPE"] == "bigintunsigned":
                row["COLUMN_TYPE"] = "bigint(20) unsigned"
            if row["COLUMN_DEFAULT"] is None:
                row["COLUMN_DEFAULT"] = "NULL"
            if row["COLUMN_DEFAULT"] == "current_timestamp":
                row["COLUMN_DEFAULT"] = "current_timestamp()"
        runtime.assert_empty_schema()
        self.assertTrue(all(call.args[0].startswith("SELECT ") for call in runtime.sql.call_args_list))
        self.raw.assert_not_called()

    def test_schema_drift_in_columns_keys_indexes_and_checks_is_rejected(self):
        runtime, metadata = self.schema_fixture()
        for category in metadata:
            original = copy.deepcopy(metadata[category])
            metadata[category].pop()
            with self.subTest(missing=category), self.assertRaises(runner.IntegrationSafetyError):
                runtime.assert_empty_schema()
            metadata[category][:] = original
        for category, field, value in (
                ("COLUMNS", "COLUMN_TYPE", "varchar(1)"), ("COLUMNS", "IS_NULLABLE", "YES"),
                ("COLUMNS", "COLUMN_DEFAULT", "'unexpected'"), ("COLUMNS", "COLLATION_NAME", "ascii_bin"),
                ("STATISTICS", "NON_UNIQUE", 1), ("STATISTICS", "SUB_PART", 1),
                ("KEY_COLUMN_USAGE", "REFERENCED_TABLE_SCHEMA", "farmsmart"),
                ("KEY_COLUMN_USAGE", "DELETE_RULE", "CASCADE"),
                ("CHECK_CONSTRAINTS", "CHECK_CLAUSE", "area_acres >= 0")):
            original = metadata[category][0][field]
            metadata[category][0][field] = value
            with self.subTest(category=category, field=field), self.assertRaises(runner.IntegrationSafetyError):
                runtime.assert_empty_schema()
            metadata[category][0][field] = original

    def test_check_comparison_preserves_grouping_and_sensitive_literal_spaces(self):
        normalize = runner._check_expression
        self.assertEqual(normalize("((`failed_attempts` <= 3))"), normalize("failed_attempts <= 3"))
        self.assertEqual(normalize("`last_input_count` between -1 and 16"), normalize("last_input_count BETWEEN -1 AND 16"))
        self.assertNotEqual(normalize("a = 1 AND (b = 2 OR c = 3)"), normalize("(a = 1 AND b = 2) OR c = 3"))
        self.assertNotEqual(normalize("LEFT(response_text,4) = 'CON '"), normalize("LEFT(response_text,4) = 'CON'"))

    def test_nonempty_schema_is_refused_without_automatic_cleanup(self):
        runtime, _ = self.schema_fixture()
        original = runtime.sql.side_effect
        runtime.sql.side_effect = lambda sql, params=(): {"rows": [{"n": 1}]} if "COUNT(*)" in sql else original(sql, params)
        with self.assertRaises(runner.IntegrationSafetyError):
            runtime.assert_empty_schema()
        self.assertTrue(all(call.args[0].startswith("SELECT ") for call in runtime.sql.call_args_list))

    def test_fixture_sql_sanitizes_failures_and_always_attempts_resource_cleanup(self):
        runtime = runner.Runtime(self.settings, self.raw, self.guard, None)
        for failure in (self.cursor.execute, self.connection.rollback, self.cursor.close, self.connection.close):
            self.connection.reset_mock()
            # An execution error exercises rollback even when rollback itself fails.
            self.cursor.execute.side_effect = ValueError("synthetic-private-SQL-value")
            failure.side_effect = ValueError("synthetic-private-cleanup-value")
            with self.assertRaises(runner.IntegrationSafetyError) as caught:
                runtime.sql("SELECT %s", ("synthetic-private-parameter",))
            self.assertNotIn("private", str(caught.exception))
            self.connection.rollback.assert_called_once_with()
            self.cursor.close.assert_called_once_with()
            self.connection.close.assert_called_once_with()
            failure.side_effect = None

    def test_case_cleanup_runs_after_success_setup_failure_and_body_errors(self):
        scenario_spec = importlib.util.spec_from_file_location("cleanup_scenarios", DIRECTORY / "scenarios.py")
        scenarios = importlib.util.module_from_spec(scenario_spec)
        with patch("mysql.connector.connect", side_effect=AssertionError("Database forbidden")) as connect:
            scenario_spec.loader.exec_module(scenarios)
            runtime = MagicMock()
            case_class = type(next(iter(scenarios.build_suite(runtime))))
            name = "test_successful_pin_replay_grants_no_new_authorization"
            for stage in ("success", "setup", "body"):
                order = []
                runtime.app.app_context.return_value.pop.side_effect = lambda: order.append("context")
                def body(case):
                    if stage == "body":
                        raise RuntimeError("synthetic-private-body-error")
                farmer = MagicMock(side_effect=RuntimeError("synthetic-private-setup-error")) if stage == "setup" else MagicMock(return_value={})
                with patch.object(case_class, name, body), patch.object(case_class, "new_farmer", farmer), \
                        patch.object(case_class, "login"), patch.object(case_class, "cleanup", lambda case: order.append("fixtures")):
                    output = io.StringIO()
                    result = unittest.TextTestRunner(stream=output, resultclass=runner.SafeResult).run(case_class(name))
                self.assertEqual(order, ["fixtures", "context"])
                self.assertEqual(result.wasSuccessful(), stage == "success")
                self.assertNotIn("synthetic-private", output.getvalue())
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
