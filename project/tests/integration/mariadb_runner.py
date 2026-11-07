"""Explicit opt-in runner. Importing this module never opens a connection."""
import ast
import base64
import contextlib
import dataclasses
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import warnings


MODE = "NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION"
TABLES = {"users", "farmers", "agricultural_officers", "farm_plots",
          "farmer_ussd_credentials", "ussd_sessions", "ussd_callback_results"}
PROTECTED = {"farmsmart", "farmsmart_test", "mysql", "information_schema",
             "performance_schema", "sys", "test"}
TIMEOUT = 10


class IntegrationSafetyError(Exception):
    """Fixed public error; never include configuration or connector diagnostics."""


@dataclasses.dataclass(frozen=True, repr=False)
class Settings:
    host: str
    port: int
    database: str
    user: str
    password: str

    def __post_init__(self):
        if (self.host not in ("localhost", "127.0.0.1") or type(self.port) is not int or self.port != 3306
                or not isinstance(self.database, str)
                or not re.fullmatch(r"farmadvis_integration_[0-9]{8}_[a-z0-9]{8,16}", self.database)
                or not isinstance(self.user, str) or not re.fullmatch(r"farmadvis_it_[a-z0-9]{4,16}", self.user)
                or not isinstance(self.password, str) or not self.password):
            raise IntegrationSafetyError("Integration target or account is not approved.")

    @classmethod
    def from_environment(cls, env):
        required = ("TEST_MYSQL_HOST", "TEST_MYSQL_PORT", "TEST_MYSQL_DATABASE",
                    "TEST_MYSQL_APPROVED_DATABASE", "TEST_MYSQL_USER", "TEST_MYSQL_PASSWORD")
        if env.get("RUN_MARIADB_INTEGRATION_TESTS") != "1" or any(
                not isinstance(env.get(key), str) or not env[key] for key in required):
            raise IntegrationSafetyError("Explicit integration opt-in and dedicated settings are required.")
        host, name, user = env[required[0]], env[required[2]], env[required[4]]
        if (host not in ("localhost", "127.0.0.1") or env[required[1]] != "3306"
                or name != env[required[3]] or name.lower() in PROTECTED
                or not re.fullmatch(r"farmadvis_integration_[0-9]{8}_[a-z0-9]{8,16}", name)
                or not re.fullmatch(r"farmadvis_it_[a-z0-9]{4,16}", user)):
            raise IntegrationSafetyError("Integration target or account is not approved.")
        return cls(host, 3306, name, user, env[required[5]])

    def connection_arguments(self):
        return dict(host=self.host, port=self.port, database=self.database,
                    user=self.user, password=self.password)


class ConnectionGuard:
    """Wrap the real connector, not the repository. Validate before and after connect."""

    def __init__(self, settings, connector, resolver=socket.getaddrinfo):
        self.settings, self.connector, self.resolver = settings, connector, resolver
        self.failed = False
        self.local = threading.local()

    def __call__(self, *args, **kwargs):
        connection = cursor = None
        try:
            expected = self.settings.connection_arguments()
            if args or set(kwargs) != set(expected) or any(
                    type(kwargs[key]) is not type(value) or kwargs[key] != value
                    for key, value in expected.items()):
                raise IntegrationSafetyError()
            addresses = self.resolver(self.settings.host, 3306, type=socket.SOCK_STREAM)
            if not addresses or not all(ipaddress.ip_address(row[4][0]).is_loopback for row in addresses):
                raise IntegrationSafetyError()
            connection = self.connector(**expected, connection_timeout=5, read_timeout=TIMEOUT,
                                        write_timeout=TIMEOUT, charset="utf8mb4",
                                        collation="utf8mb4_unicode_ci", get_warnings=True)
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT DATABASE() AS db, VERSION() AS version, @@port AS port, "
                           "@@sql_mode AS mode, @@tx_isolation AS isolation, "
                           "@@foreign_key_checks AS fk, @@check_constraint_checks AS checks, "
                           "CURRENT_USER() AS account, CURRENT_ROLE() AS role")
            row = cursor.fetchone()
            if (row["db"] != self.settings.database or row["version"] != "10.4.28-MariaDB"
                    or row["port"] != 3306 or row["mode"] != MODE
                    or row["isolation"] != "REPEATABLE-READ" or row["fk"] != 1 or row["checks"] != 1
                    or row["role"] not in (None, "NONE") or not isinstance(row["account"], str)):
                raise IntegrationSafetyError()
            account, host = row["account"].rsplit("@", 1)
            if account != self.settings.user or not re.fullmatch(r"[A-Za-z0-9_.:%-]+", host):
                raise IntegrationSafetyError()
            grantee = "'" + account + "'@'" + host + "'"
            # Unlike SHOW GRANTS, these metadata queries do not retrieve password hashes.
            cursor.execute("SELECT PRIVILEGE_TYPE, IS_GRANTABLE FROM information_schema.USER_PRIVILEGES WHERE GRANTEE=%s", (grantee,))
            if any(r["PRIVILEGE_TYPE"] != "USAGE" or r["IS_GRANTABLE"] != "NO" for r in cursor.fetchall()):
                raise IntegrationSafetyError()
            cursor.execute("SELECT TABLE_SCHEMA, PRIVILEGE_TYPE, IS_GRANTABLE FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE=%s", (grantee,))
            grants = cursor.fetchall()
            allowed = {"SELECT", "INSERT", "UPDATE", "DELETE"}
            if ({r["PRIVILEGE_TYPE"] for r in grants} != allowed or any(
                    r["TABLE_SCHEMA"] != self.settings.database.replace("_", "\\_")
                    or r["IS_GRANTABLE"] != "NO" for r in grants)):
                raise IntegrationSafetyError()
            for table in ("TABLE_PRIVILEGES", "COLUMN_PRIVILEGES"):
                cursor.execute("SELECT COUNT(*) AS n FROM information_schema." + table + " WHERE GRANTEE=%s", (grantee,))
                if cursor.fetchone()["n"] != 0:
                    raise IntegrationSafetyError()
            zone = getattr(self.local, "zone", None)
            if zone is not None:
                if zone not in ("+00:00", "+03:00"):
                    raise IntegrationSafetyError()
                cursor.execute("SET SESSION time_zone=%s", (zone,))
            cursor.close()
            cursor = None
            connection.rollback()  # Return a fresh connection for service-owned transactions.
            return connection
        except Exception:
            self.failed = True
            for resource in (cursor, connection):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            raise IntegrationSafetyError("MariaDB integration connection refused.") from None


def synthetic_config(settings):
    return dict(TESTING=True, DEBUG=False, SECRET_KEY=secrets.token_hex(32),
                MYSQL_HOST=settings.host, MYSQL_PORT=settings.port,
                MYSQL_DATABASE=settings.database, MYSQL_USER=settings.user, MYSQL_PASSWORD=settings.password,
                USSD_PIN_PEPPERS=json.dumps({"1": base64.b64encode(secrets.token_bytes(32)).decode("ascii")}),
                USSD_PIN_CURRENT_KEY_VERSION="1",
                USSD_METADATA_HMAC_KEY=base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
                USSD_SERVICE_CODE="*384*999#", USSD_PERSISTENT_FLOW_ENABLED=False,
                USSD_RELAY_SECRET=None, USSD_TRUSTED_RELAY_IPS=None, AFRICASTALKING_API_KEY=None,
                SESSION_COOKIE_SECURE=False)


def _sql_type(value):
    # MariaDB reports integer display widths, which do not change storage types.
    value = re.sub(r"\b(bigint|smallint|tinyint)\(\d+\)", r"\1", value.lower())
    return re.sub(r"\s+", "", value)


def _default(value):
    if value is None or value == "NULL":
        return None
    return str(value).replace("CURRENT_TIMESTAMP", "current_timestamp").replace("current_timestamp()", "current_timestamp")


def _check_expression(value):
    # Compare expression structure, not parentheses/spacing rendered by MariaDB.
    # ast.parse does not execute the expression. Preserve quoted literal values.
    parts = re.split(r"('(?:''|[^'])*')", value.replace("`", ""))
    for index in range(0, len(parts), 2):
        text = parts[index].lower()
        text = re.sub(r"\b_\w+$", "", text)  # Optional SQL string introducer.
        text = re.sub(r"\b(\w+) between (-?\d+) and (-?\d+)", r"(\2 <= \1 <= \3)", text)
        text = text.replace("<>", "!=")
        text = re.sub(r"(?<![<>=!])=(?!=)", "==", text)
        parts[index] = re.sub(r"\s+", " ", re.sub(r"\bnull\b", "None", text))
    return ast.dump(ast.parse("".join(parts).strip(), mode="eval"), include_attributes=False)


def _schema_clauses(body):
    # Only the checked-in CREATE TABLE grammar is read; SQL is never executed.
    clauses, current, depth = [], [], 0
    for token in re.findall(r"'(?:''|[^'])*'|[(),]|[^'(),]+", body):
        if token == "," and depth == 0:
            clauses.append("".join(current).strip())
            current = []
            continue
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        current.append(token)
    clauses.append("".join(current).strip())
    return clauses


def approved_schema(database):
    """Small metadata contract derived from the approved fresh-install schema."""
    source = (Path(__file__).resolve().parents[2] / "schema" / "schema.sql").read_text(encoding="utf-8")
    source = re.sub(r"--[^\n]*", "", source)
    tables = re.findall(r"CREATE TABLE (\w+) \((.*?)\) ENGINE=InnoDB DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;", source, re.S)
    if {table for table, _ in tables} != TABLES:
        raise IntegrationSafetyError("Approved schema contract is unavailable.")
    expected = {}
    for table, body in tables:
        for clause in _schema_clauses(body):
            column = re.fullmatch(r"([a-z_]+) ([A-Z]+(?:\([^)]*\))?(?: UNSIGNED)?)\s+(.*)", clause, re.S)
            index = re.fullmatch(r"(?:PRIMARY KEY|INDEX (\w+)|CONSTRAINT (\w+) UNIQUE) \(([^)]+)\)", clause)
            foreign = re.fullmatch(r"CONSTRAINT (\w+)\s+FOREIGN KEY \((\w+)\)\s+REFERENCES (\w+) \((\w+)\)\s+ON DELETE (RESTRICT|CASCADE) ON UPDATE RESTRICT", clause)
            check = re.fullmatch(r"CONSTRAINT (\w+) CHECK \((.*)\)", clause, re.S)
            if column:
                name, kind, attributes = column.groups()
                default = re.search(r"DEFAULT ('[^']*'|-?\d+|CURRENT_TIMESTAMP)", attributes)
                collation = "ascii_bin" if "COLLATE ascii_bin" in attributes else (
                    "utf8mb4_unicode_ci" if kind.startswith(("VARCHAR", "ENUM")) else None)
                extra = "auto_increment" if "AUTO_INCREMENT" in attributes else (
                    "on update current_timestamp" if "ON UPDATE CURRENT_TIMESTAMP" in attributes else "")
                expected[("column", table, name)] = (_sql_type(kind), "NO" if "NOT NULL" in attributes else "YES",
                                                       _default(default[1]) if default else None, extra, collation)
            elif index:
                ordinary, unique, columns = index.groups()
                for position, name in enumerate(columns.replace(" ", "").split(","), 1):
                    expected[("index", table, ordinary or unique or "PRIMARY", position)] = (name, int(ordinary is not None), None, "BTREE")
            elif foreign:
                name, column, target, target_column, delete = foreign.groups()
                expected[("foreign", table, name, 1)] = (column, database, target, target_column, delete, "RESTRICT")
            elif check:
                expected[("check", table, check[1])] = _check_expression(check[2])
            else:
                raise IntegrationSafetyError("Approved schema contract is unsupported.")
    return expected


class Runtime:
    def __init__(self, settings, connector, guard, app):
        self.settings, self.connector, self.guard, self.app = settings, connector, guard, app

    def sql(self, statement, parameters=()):
        cn = cursor = None
        try:
            cn = self.connector(**self.settings.connection_arguments())
            cursor = cn.cursor(dictionary=True)
            cursor.execute(statement, parameters)
            result = dict(rows=cursor.fetchall() if cursor.with_rows else [],
                          lastrowid=cursor.lastrowid, warnings=cursor.warning_count)
            cn.commit()
            return result
        except Exception:
            if cn is not None:
                try:
                    cn.rollback()
                except Exception:
                    pass
            raise IntegrationSafetyError("Synthetic fixture operation failed.") from None
        finally:
            failed = False
            for resource in (cursor, cn):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        failed = True
            if failed:
                raise IntegrationSafetyError("Synthetic fixture cleanup failed.") from None

    @contextlib.contextmanager
    def timezone(self, zone):
        if zone not in ("+00:00", "+03:00"):
            raise IntegrationSafetyError("Unsupported test timezone.")
        previous = getattr(self.guard.local, "zone", None)
        self.guard.local.zone = zone
        try:
            yield
        finally:
            self.guard.local.zone = previous

    def assert_empty_schema(self):
        rows = self.sql("SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME")["rows"]
        if {r["TABLE_NAME"] for r in rows} != TABLES or any(r["ENGINE"] != "InnoDB" for r in rows):
            raise IntegrationSafetyError("Integration schema must match the approved fresh schema.")
        expected, actual = approved_schema(self.settings.database), {}
        for r in self.sql("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE()")["rows"]:
            actual[("column", r["TABLE_NAME"], r["COLUMN_NAME"])] = (
                _sql_type(r["COLUMN_TYPE"]), r["IS_NULLABLE"], _default(r["COLUMN_DEFAULT"]),
                r["EXTRA"].replace("current_timestamp()", "current_timestamp"), r["COLLATION_NAME"])
        for r in self.sql("SELECT TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, SUB_PART, INDEX_TYPE FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=DATABASE()")["rows"]:
            actual[("index", r["TABLE_NAME"], r["INDEX_NAME"], r["SEQ_IN_INDEX"])] = (
                r["COLUMN_NAME"], r["NON_UNIQUE"], r["SUB_PART"], r["INDEX_TYPE"])
        for r in self.sql("SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.DELETE_RULE, r.UPDATE_RULE FROM information_schema.KEY_COLUMN_USAGE k JOIN information_schema.REFERENTIAL_CONSTRAINTS r ON r.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA AND r.TABLE_NAME=k.TABLE_NAME AND r.CONSTRAINT_NAME=k.CONSTRAINT_NAME WHERE k.TABLE_SCHEMA=DATABASE() AND k.REFERENCED_TABLE_NAME IS NOT NULL")["rows"]:
            actual[("foreign", r["TABLE_NAME"], r["CONSTRAINT_NAME"], r["ORDINAL_POSITION"])] = (
                r["COLUMN_NAME"], r["REFERENCED_TABLE_SCHEMA"], r["REFERENCED_TABLE_NAME"],
                r["REFERENCED_COLUMN_NAME"], r["DELETE_RULE"], r["UPDATE_RULE"])
        for r in self.sql("SELECT TABLE_NAME, CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS WHERE CONSTRAINT_SCHEMA=DATABASE()")["rows"]:
            actual[("check", r["TABLE_NAME"], r["CONSTRAINT_NAME"])] = _check_expression(r["CHECK_CLAUSE"])
        if actual != expected:
            raise IntegrationSafetyError("Integration columns or constraints do not match the approved schema.")
        for table in sorted(TABLES):
            if self.sql("SELECT COUNT(*) AS n FROM `" + table + "`")["rows"][0]["n"] != 0:
                raise IntegrationSafetyError("Integration database is not empty; refusing automatic reset.")


class SafeResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        # Drivers/assertions may include SQL values or hashes. Never print tracebacks.
        return "Integration check failed; exception and record details suppressed.\n"


@contextlib.contextmanager
def isolated_runtime(settings):
    if any(name == "app" or name.startswith("app.") for name in sys.modules):
        raise IntegrationSafetyError("Run MariaDB integration tests in a fresh Python process.")
    import dotenv
    import mysql.connector
    guard = ConnectionGuard(settings, mysql.connector.connect)
    config = synthetic_config(settings)
    env = {key: val for key, val in os.environ.items()
           if not key.startswith(("MYSQL_", "USSD_", "AFRICASTALKING_"))
           and key not in ("SECRET_KEY", "SESSION_COOKIE_SECURE", "AT_API_KEY", "AFRICAS_TALKING_API_KEY")}
    env.update({key: str(config[key]) for key in ("MYSQL_HOST", "MYSQL_PORT", "MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD", "SECRET_KEY")})
    env["USSD_PERSISTENT_FLOW_ENABLED"] = "false"
    with patch.dict(os.environ, env, clear=True), patch.object(dotenv, "load_dotenv", return_value=False), \
            patch.object(mysql.connector, "connect", guard), warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # Counts are asserted; warning text can contain SQL values.
        from app import create_app
        app = create_app()
        app.config.update(config)
        yield Runtime(settings, mysql.connector.connect, guard, app)


def main():
    try:
        settings = Settings.from_environment(os.environ)
        project = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(project))
        with isolated_runtime(settings) as runtime:
            runtime.assert_empty_schema()
            from scenarios import build_suite
            result = unittest.TextTestRunner(verbosity=2, resultclass=SafeResult).run(build_suite(runtime))
            runtime.assert_empty_schema()
            if runtime.guard.failed or not result.wasSuccessful():
                return 1
        return 0
    except Exception:
        print("MariaDB integration run refused or failed; configuration and database details suppressed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
