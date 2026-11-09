"""Real-MariaDB scenarios, loaded only by the opted-in guarded runner."""
import base64
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
import secrets
import sys
import time
from threading import Barrier, Event, Lock
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def build_suite(runtime):
    # All application imports occur after the runner installs connection/env guards.
    from werkzeug.security import generate_password_hash
    from app.models import ussd_repository as repository
    from app.models.ussd_repository import SAFE_RESPONSES
    from app.services import registration_service as registration
    from app.services import farmer_ussd_pin_service as pin_management
    from app.services import ussd_authentication_service as authentication
    from app.services.farm_plot_service import update_farmer_plot
    from app.services.ussd_callback_service import orchestrate_callback
    from app.models.ussd_fingerprints import derive_session_key
    from app.services.ussd_pin_service import verify_ussd_pin
    from app.db import get_ussd_db_connection
    from mysql.connector.errors import Error, ReadTimeoutError

    class MariaDbFeatureTests(unittest.TestCase):
        password = "synthetic-integration-password"

        @classmethod
        def setUpClass(cls):
            cls.web_hash = generate_password_hash(cls.password, method="scrypt")

        def setUp(self):
            self.context = runtime.app.app_context()
            self.context.push()
            self.addCleanup(self.context.pop)
            self.prefix = "IT_" + secrets.token_hex(8) + "_"
            self.counter, self.mutex, self.session_keys = 0, Lock(), []
            self.addCleanup(self.cleanup)
            self.first = self.new_farmer()
            self.second = self.new_farmer()
            self.client = self.login(self.first)

        def name(self):
            with self.mutex:
                self.counter += 1
                return self.prefix + str(self.counter)

        def payload(self, phone=None):
            return dict(first_name=self.name(), last_name="Synthetic", phone_number=phone or
                        "+2547" + f"{secrets.randbelow(100000000):08d}", password=self.password,
                        password_confirmation=self.password)

        def new_farmer(self):
            data = self.payload()
            result = registration.register_farmer(data)
            self.assertTrue(result["ok"], "Synthetic farmer setup failed.")
            return dict(user_id=result["user_id"], farmer_id=result["farmer_id"], phone=data["phone_number"])

        def rows(self, statement, params=()):
            return runtime.sql(statement, params)["rows"]

        def cleanup(self):
            # Service commits use separate connections: clean only this case's
            # tagged users/children and explicitly tracked unbound session keys.
            users = [r["user_id"] for r in self.rows("SELECT user_id FROM users WHERE LEFT(first_name,%s)=%s", (len(self.prefix), self.prefix))]
            marks = lambda ids: ",".join("%s" for _ in ids)
            farmers = []
            if users:
                farmers = [r["farmer_id"] for r in self.rows("SELECT farmer_id FROM farmers WHERE user_id IN (" + marks(users) + ")", tuple(users))]
            clauses, args = [], []
            if farmers:
                clauses.append("farmer_id IN (" + marks(farmers) + ")")
                args.extend(farmers)
            if self.session_keys:
                clauses.append("session_key IN (" + marks(self.session_keys) + ")")
                args.extend(self.session_keys)
            if clauses:
                runtime.sql("DELETE FROM ussd_sessions WHERE " + " OR ".join(clauses), tuple(args))
            if farmers:
                for table in ("farmer_ussd_credentials", "farm_plots"):
                    runtime.sql("DELETE FROM " + table + " WHERE farmer_id IN (" + marks(farmers) + ")", tuple(farmers))
            if users:
                for table in ("farmers", "agricultural_officers", "users"):
                    runtime.sql("DELETE FROM " + table + " WHERE user_id IN (" + marks(users) + ")", tuple(users))

        def login(self, farmer):
            client = runtime.app.test_client()
            token = client.get("/farmer/login").json["csrf_token"]
            response = client.post("/farmer/login", json=dict(phone_number=farmer["phone"], password=self.password), headers={"X-CSRF-Token": token})
            self.assertEqual(response.status_code, 200)
            return client

        def plot(self, client=None, **changes):
            client = client or self.client
            data = dict(plot_name="Synthetic plot", area_acres="1.125", county="Uasin Gishu", sub_county="Moiben", ward="Moiben")
            data.update(changes)
            token = client.get("/farmer/plots/csrf-token").json["csrf_token"]
            return client.post("/farmer/plots", json=data, headers={"X-CSRF-Token": token})

        def pin(self, farmer=None, pin="0042", password=None, client=None):
            client = client or (self.client if farmer is None else self.login(farmer))
            html = client.get("/farmer/home").get_data(as_text=True)
            token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
            return client.post("/farmer/ussd-pin", data=dict(csrf_token=token, web_password=self.password if password is None else password, ussd_pin=pin, pin_confirmation=pin))

        def credential(self, farmer=None):
            return self.rows("SELECT * FROM farmer_ussd_credentials WHERE farmer_id=%s", ((farmer or self.first)["farmer_id"],))[0]

        def start_pin(self, farmer=None):
            farmer = farmer or self.first
            sid = "it-" + secrets.token_hex(16)
            code = runtime.app.config["USSD_SERVICE_CODE"]
            key = base64.b64decode(runtime.app.config["USSD_METADATA_HMAC_KEY"])
            session_key = derive_session_key(key, code, sid)
            with self.mutex:
                self.session_keys.append(session_key)
            settings = SimpleNamespace(service_code=code, metadata_key=key, session_seconds=180, retention_seconds=86400)
            self.assertEqual(orchestrate_callback(settings, sid, code, farmer["phone"], ""), SAFE_RESPONSES["welcome"])
            self.assertEqual(authentication.identify_ussd_candidate(farmer["phone"], sid, code)["response"], SAFE_RESPONSES["pin_prompt"])
            return sid, session_key

        def authenticate(self, sid, pin="0042", farmer=None):
            farmer = farmer or self.first
            return authentication.authenticate_ussd_pin(farmer["phone"], sid, runtime.app.config["USSD_SERVICE_CODE"], "1*" + pin)

        def session(self, key):
            return self.rows("SELECT * FROM ussd_sessions WHERE session_key=%s", (key,))[0]

        @contextmanager
        def timeout_cursor(self):
            """Own a guarded USSD connection; always discard its transaction."""
            connection = cursor = None
            try:
                connection = get_ussd_db_connection()
                connection.start_transaction()
                cursor = connection.cursor(dictionary=True)
                yield cursor
            finally:
                active_failure = sys.exc_info()[0] is not None
                failed = close_failed = False
                operations = []
                if connection is not None:
                    operations.append(("rollback", connection.rollback))
                if cursor is not None:
                    operations.append(("cursor", cursor.close))
                if connection is not None:
                    operations.append(("connection", connection.close))
                for label, operation in operations:
                    try:
                        operation()
                    except Exception as error:
                        failed = True
                        close_failed |= label == "connection"
                        self.record_timeout_diagnostic("cleanup_" + label, error)
                # A socket timeout may already have disconnected the client.
                # Still attempt all cleanup; preserve the original error only
                # when connection.close() succeeds, never conceal a close failure.
                if close_failed or (failed and not active_failure):
                    raise RuntimeError("Timeout scenario cleanup failed.") from None

        def record_timeout_diagnostic(self, phase, error, elapsed=None):
            # Keep no message, SQL, parameters or records. SafeResult validates
            # these fields again before rendering them, including on failures.
            if not hasattr(self, "timeout_diagnostics"):
                self.timeout_diagnostics = []
            self.timeout_diagnostics.append(dict(
                phase=phase, errno=getattr(error, "errno", None),
                sqlstate=getattr(error, "sqlstate", None),
                exception=type(error).__name__, elapsed=elapsed))

        def test_ussd_timeout_session_values_and_repository_initialization(self):
            statement = ("SELECT @@SESSION.innodb_lock_wait_timeout AS lock_seconds, "
                         "@@SESSION.max_statement_time AS statement_seconds")
            fixture_before = self.rows(statement)[0]
            with self.timeout_cursor() as cursor:
                cursor.execute(statement)
                values = cursor.fetchone()
                self.assertEqual(values["lock_seconds"], 2)
                self.assertEqual(float(values["statement_seconds"]), 3.0)
                cursor.execute("SELECT DATABASE() AS db")
                self.assertEqual(cursor.fetchone()["db"], runtime.settings.database)
            with repository.ussd_transaction() as store:
                self.assertIsNone(store.get_credentials(self.first["farmer_id"]))
            self.assertEqual(self.rows(statement)[0], fixture_before)

        def test_ussd_injected_initialization_and_query_errors_are_safe(self):
            with repository.ussd_transaction() as store:
                store.ensure_credentials(self.first["farmer_id"], datetime.now(timezone.utc))
            before = self.credential()
            original_connector = runtime.guard.connector
            for target in ("SET SESSION max_statement_time", "UPDATE farmer_ussd_credentials"):
                opened = []
                class CursorProxy:
                    def __init__(inner, cursor):
                        inner.cursor = cursor
                    def __getattr__(inner, name):
                        return getattr(inner.cursor, name)
                    def execute(inner, sql, params=()):
                        result = inner.cursor.execute(sql, params)
                        if sql.startswith(target):
                            raise Error("Synthetic injected SQL failure")
                        return result
                class ConnectionProxy:
                    def __init__(inner, connection):
                        inner.connection, inner.closed = connection, False
                    def __getattr__(inner, name):
                        return getattr(inner.connection, name)
                    def cursor(inner, *args, **kwargs):
                        return CursorProxy(inner.connection.cursor(*args, **kwargs))
                    def close(inner):
                        inner.connection.close()
                        inner.closed = True
                def connect(**kwargs):
                    wrapped = ConnectionProxy(original_connector(**kwargs))
                    opened.append(wrapped)
                    return wrapped
                with patch.object(runtime.guard, "connector", side_effect=connect):
                    with self.assertRaises(repository.UssdPersistenceError):
                        with repository.ussd_transaction() as store:
                            store.set_attempt_state(self.first["farmer_id"], 1, None, datetime.now(timezone.utc))
                self.assertEqual(len(opened), 1)
                self.assertTrue(all(connection.closed for connection in opened))
                self.assertEqual(self.credential(), before, "Injected failure committed attempt state.")

        def test_ussd_lock_wait_timeout_rolls_back_synthetic_update(self):
            before = self.rows("SELECT last_name FROM users WHERE user_id=%s", (self.second["user_id"],))[0]
            # Isolate the lock timeout from competing finite statement/socket
            # deadlines. Restore configuration even on setup or cleanup errors.
            with patch.dict(runtime.app.config, {"USSD_DB_LOCK_WAIT_TIMEOUT": 2,
                            "USSD_DB_STATEMENT_TIMEOUT": 5, "USSD_DB_READ_TIMEOUT": 10}):
                with self.timeout_cursor() as holder:
                    holder.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (self.first["user_id"],))
                    self.assertIsNotNone(holder.fetchone())
                    with self.timeout_cursor() as waiter:
                        waiter.execute("UPDATE users SET last_name='Synthetic pending' WHERE user_id=%s", (self.second["user_id"],))
                        started = time.monotonic()
                        observed = None
                        try:
                            waiter.execute("SELECT user_id FROM users WHERE user_id=%s FOR UPDATE", (self.first["user_id"],))
                            waiter.fetchall()
                        except Exception as error:
                            observed = error
                            if not isinstance(error, Error):
                                raise
                        finally:
                            elapsed = time.monotonic() - started
                            self.record_timeout_diagnostic("lock_wait_select", observed, elapsed)
            # Read back only after both transactions have rolled back and both
            # connections have closed, even if an unexpected SQL errno occurred.
            self.assertEqual(self.rows("SELECT last_name FROM users WHERE user_id=%s", (self.second["user_id"],))[0], before)
            self.assertIsNotNone(observed, "Expected a lock-wait error.")
            self.assertEqual(observed.errno, 1205)
            self.assertTrue(1 <= elapsed <= 10, "Unexpected lock timeout duration.")

        def test_ussd_statement_timeout_aborts_bounded_read_only_query(self):
            with self.assertRaises(Error) as caught:
                with self.timeout_cursor() as cursor:
                    started = time.monotonic()
                    cursor.execute("SELECT SLEEP(6) AS bounded_wait")
                    cursor.fetchall()
            elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.errno, 1969)
            self.assertTrue(2 <= elapsed <= 10, "Unexpected statement timeout duration.")

        def test_ussd_client_read_timeout_uses_scoped_test_configuration(self):
            # Keep a finite server timeout, but give the client a clear head start.
            # No server shutdown, remote target or network modification is used.
            with patch.dict(runtime.app.config, {"USSD_DB_READ_TIMEOUT": 1, "USSD_DB_STATEMENT_TIMEOUT": 5}):
                with self.assertRaises(Error) as caught:
                    with self.timeout_cursor() as cursor:
                        started = time.monotonic()
                        cursor.execute("SELECT SLEEP(6) AS bounded_wait")
                        cursor.fetchall()
                elapsed = time.monotonic() - started
                self.assertTrue(isinstance(caught.exception, ReadTimeoutError)
                                or caught.exception.errno in (2013, 2055), "Expected a client read failure.")
                self.assertTrue(0.5 <= elapsed <= 10, "Unexpected socket timeout duration.")

        def test_registration_login_normalization_duplicate_and_linkage(self):
            data = self.payload()
            canonical = data["phone_number"]
            data["phone_number"] = "0" + canonical[4:]
            response = runtime.app.test_client().post("/farmer/register", json=data)
            self.assertEqual(response.status_code, 201)
            ids = response.json
            row = self.rows("SELECT u.phone_number,u.role,u.status,f.user_id,f.farmer_id FROM users u JOIN farmers f ON f.user_id=u.user_id WHERE u.user_id=%s", (ids["user_id"],))[0]
            self.assertTrue(row["phone_number"] == canonical and row["user_id"] == ids["user_id"] and row["farmer_id"] == ids["farmer_id"])
            self.assertEqual((row["role"], row["status"]), ("farmer", "active"))
            self.assertEqual(self.login(dict(phone=canonical)).get("/farmer/home").status_code, 200)
            duplicate = registration.register_farmer(dict(data, phone_number=canonical))
            self.assertEqual(duplicate["code"], "duplicate_phone")

        def test_multiple_plots_locations_ownership_and_decimal_readback(self):
            first = self.plot()
            second = self.plot(sub_county="Turbo", ward="Kamagut", area_acres="0.001")
            self.assertEqual((first.status_code, second.status_code), (201, 201))
            listed = self.client.get("/farmer/plots").json["plots"]
            self.assertEqual(len(listed), 2)
            self.assertEqual({p["area_acres"] for p in listed}, {"1.125000", "0.001000"})
            self.assertTrue(all(p["county"] == "Uasin Gishu" for p in listed))
            for changes in (dict(ward="Langas"), dict(county="Trans Nzoia"), dict(sub_county="Kitale"), dict(area_acres="1.1234")):
                self.assertEqual(self.plot(**changes).status_code, 400)
            token = self.client.get("/farmer/plots/csrf-token").json["csrf_token"]
            edited = self.client.patch("/farmer/plots/" + str(first.json["plot_id"]), json=dict(county="Uasin Gishu", sub_county="Kapseret", ward="Langas"), headers={"X-CSRF-Token": token})
            self.assertEqual(edited.status_code, 200)
            self.assertEqual(self.rows("SELECT ward,farmer_id FROM farm_plots WHERE plot_id=%s", (first.json["plot_id"],))[0], {"ward": "Langas", "farmer_id": self.first["farmer_id"]})

        def test_legacy_nulls_unlisted_text_and_six_decimal_area_survive_edit(self):
            plot_id = runtime.sql("INSERT INTO farm_plots (farmer_id,plot_name,area_acres,soil_type,farming_type) VALUES (%s,'Synthetic legacy','1.123456','Legacy soil','Legacy water')", (self.first["farmer_id"],))["lastrowid"]
            token = self.client.get("/farmer/plots/csrf-token").json["csrf_token"]
            result = self.client.patch("/farmer/plots/" + str(plot_id), json={"location": "Synthetic landmark"}, headers={"X-CSRF-Token": token})
            self.assertEqual(result.status_code, 200)
            row = self.rows("SELECT area_acres,soil_type,farming_type,county,sub_county,ward FROM farm_plots WHERE plot_id=%s", (plot_id,))[0]
            self.assertTrue(row == dict(area_acres=Decimal("1.123456"), soil_type="Legacy soil", farming_type="Legacy water", county=None, sub_county=None, ward=None))

        def test_authentication_role_csrf_and_cross_farmer_updates(self):
            plot_id = self.plot().json["plot_id"]
            other = self.login(self.second)
            token = other.get("/farmer/plots/csrf-token").json["csrf_token"]
            self.assertEqual(other.patch("/farmer/plots/" + str(plot_id), json={"plot_name": "Forged"}, headers={"X-CSRF-Token": token}).status_code, 404)
            self.assertEqual(other.get("/farmer/plots").json["plots"], [])
            self.assertEqual(self.client.post("/farmer/plots", json={}).status_code, 403)
            self.assertEqual(runtime.app.test_client().get("/farmer/plots").status_code, 401)
            self.assertEqual(self.client.post("/farmer/ussd-pin", data=dict(csrf_token="invalid", web_password=self.password, ussd_pin="0042", pin_confirmation="0042")).status_code, 403)
            runtime.sql("UPDATE users SET role='officer' WHERE user_id=%s", (self.first["user_id"],))
            self.assertEqual(self.client.get("/farmer/home").status_code, 401)

        def test_officer_visibility_is_assignment_scoped(self):
            plot_id = self.plot().json["plot_id"]
            email = self.name() + "@example.invalid"
            user_id = runtime.sql("INSERT INTO users (first_name,last_name,email,password_hash,role,status) VALUES (%s,'Synthetic',%s,%s,'officer','active')", (self.name(), email, self.web_hash))["lastrowid"]
            officer_id = runtime.sql("INSERT INTO agricultural_officers (user_id,approved_at) VALUES (%s,UTC_TIMESTAMP())", (user_id,))["lastrowid"]
            runtime.sql("UPDATE farmers SET assigned_officer_id=%s WHERE farmer_id=%s", (officer_id, self.first["farmer_id"]))
            client = runtime.app.test_client()
            token = client.get("/officer/login").json["csrf_token"]
            self.assertEqual(client.post("/officer/login", json=dict(email=email, password=self.password), headers={"X-CSRF-Token": token}).status_code, 200)
            response = client.get("/officer/api/farmers/" + str(self.first["farmer_id"]) + "/plots")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["plots"][0]["plot_id"], plot_id)
            self.assertEqual(client.get("/officer/api/farmers/" + str(self.second["farmer_id"]) + "/plots").status_code, 404)

        def test_pin_enrollment_leading_zero_change_and_scoped_invalidation(self):
            self.assertEqual(self.pin().status_code, 303)
            self.assertEqual(self.pin(self.second).status_code, 303)
            sid, key = self.start_pin()
            self.assertTrue(self.authenticate(sid)["authenticated"])
            other_sid, other_key = self.start_pin(self.second)
            self.assertTrue(self.authenticate(other_sid, farmer=self.second)["authenticated"])
            before = self.credential()
            response = self.pin(pin="0000")
            self.assertEqual(response.status_code, 303)
            after = self.credential()
            self.assertEqual(after["pin_version"], before["pin_version"] + 1)
            self.assertTrue(verify_ussd_pin(self.first["farmer_id"], "0000", after["pin_hash"], after["pin_key_version"]))
            self.assertFalse(verify_ussd_pin(self.first["farmer_id"], "0042", after["pin_hash"], after["pin_key_version"]))
            self.assertEqual(self.session(key)["state"], "ended")
            self.assertIsNone(self.session(key)["verified_pin_version"])
            self.assertIsNone(self.session(key)["authenticated_at"])
            self.assertEqual(self.session(other_key)["state"], "verified")
            self.assertFalse(self.authenticate(sid)["authenticated"])
            body = self.client.get("/farmer/home").get_data(as_text=True)
            self.assertTrue(all(secret not in body for secret in (self.password, "0000", after["pin_hash"])), "Sensitive input appeared in a response.")

        def test_web_password_lockout_is_separate_and_expires(self):
            self.assertEqual(self.pin().status_code, 303)
            for number in range(5):
                self.assertEqual(self.pin(password="wrong-synthetic-password").status_code, 429 if number == 4 else 403)
            row = self.credential()
            self.assertEqual((row["web_failed_attempts"], row["failed_attempts"]), (5, 0))
            self.assertEqual(self.pin().status_code, 429)
            self.assertEqual(self.login(self.first).get("/farmer/home").status_code, 200)
            sid, _ = self.start_pin()
            self.assertTrue(self.authenticate(sid)["authenticated"])
            runtime.sql("UPDATE farmer_ussd_credentials SET web_locked_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE farmer_id=%s", (self.first["farmer_id"],))
            self.assertEqual(self.pin(pin="0000").status_code, 303)
            self.assertEqual(self.credential()["web_failed_attempts"], 0)

        def test_pin_input_validation_prevents_credential_creation(self):
            for pin, confirmation in (("123", "123"), ("12345", "12345"), ("１２３４", "１２３４"), ("0042", "0043")):
                body = self.client.get("/farmer/home").get_data(as_text=True)
                token = re.search(r'name="csrf_token" value="([^"]+)"', body).group(1)
                response = self.client.post("/farmer/ussd-pin", data=dict(csrf_token=token, web_password=self.password, ussd_pin=pin, pin_confirmation=confirmation))
                self.assertEqual(response.status_code, 400)
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM farmer_ussd_credentials WHERE farmer_id=%s", (self.first["farmer_id"],))[0]["n"], 0)

        def test_successful_pin_replay_grants_no_new_authorization(self):
            self.assertEqual(self.pin().status_code, 303)
            sid, key = self.start_pin()
            self.assertTrue(self.authenticate(sid)["authenticated"])
            before = self.credential()
            replay = self.authenticate(sid)
            self.assertFalse(replay["authenticated"])
            self.assertEqual(replay["response"], SAFE_RESPONSES["pin_verified"])
            self.assertTrue(self.credential() == before, "Replay altered credential state.")
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM ussd_callback_results WHERE session_key=%s", (key,))[0]["n"], 3)

        def test_wrong_pin_three_attempt_lockout_duplicates_and_expiry(self):
            self.assertEqual(self.pin().status_code, 303)
            for number in range(3):
                sid, key = self.start_pin()
                self.assertFalse(self.authenticate(sid, "9999")["authenticated"])
                self.assertFalse(self.authenticate(sid, "9999")["authenticated"])
                self.assertEqual(self.credential()["failed_attempts"], number + 1)
            row = self.credential()
            delta = row["locked_until"].replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
            self.assertTrue(890 <= delta.total_seconds() <= 901, "Unexpected lockout deadline.")
            sid, _ = self.start_pin()
            self.assertFalse(self.authenticate(sid)["authenticated"])
            self.assertEqual(self.credential()["failed_attempts"], 3)
            runtime.sql("UPDATE farmer_ussd_credentials SET locked_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE farmer_id=%s", (self.first["farmer_id"],))
            sid, _ = self.start_pin()
            self.assertTrue(self.authenticate(sid)["authenticated"])
            self.assertEqual(self.credential()["failed_attempts"], 0)
            self.assertIsNone(self.credential()["locked_until"])

        def test_expired_session_does_not_authenticate_or_count_a_failure(self):
            self.assertEqual(self.pin().status_code, 303)
            sid, key = self.start_pin()
            runtime.sql("UPDATE ussd_sessions SET created_at=UTC_TIMESTAMP(6)-INTERVAL 10 MINUTE,expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE session_key=%s", (key,))
            self.assertFalse(self.authenticate(sid)["authenticated"])
            self.assertEqual(self.credential()["failed_attempts"], 0)

        def test_registration_failure_rolls_back_real_user_insert(self):
            data = self.payload()
            with patch.object(registration, "create_farmer_profile", side_effect=RuntimeError("Synthetic failure")):
                self.assertFalse(registration.register_farmer(data)["ok"])
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM users WHERE first_name=%s", (data["first_name"],))[0]["n"], 0)

        def test_pin_invalidation_failure_rolls_back_hash_version_and_sessions(self):
            self.assertEqual(self.pin().status_code, 303)
            sid, key = self.start_pin()
            self.assertTrue(self.authenticate(sid)["authenticated"])
            before, session = self.credential(), self.session(key)
            original = repository._UssdRepository.end_farmer_sessions
            def fail(store, farmer):
                original(store, farmer)
                raise RuntimeError("Synthetic failure after actual invalidation SQL")
            with patch.object(repository._UssdRepository, "end_farmer_sessions", fail):
                self.assertEqual(self.pin(pin="0000").status_code, 503)
            self.assertTrue(self.credential() == before and self.session(key) == session, "PIN transaction was not rolled back.")

        def test_expiry_at_final_sql_rolls_back_callback_and_attempt_reset(self):
            self.assertEqual(self.pin().status_code, 303)
            sid, key = self.start_pin()
            runtime.sql("UPDATE ussd_sessions SET created_at=UTC_TIMESTAMP(6)-INTERVAL 1 MINUTE WHERE session_key=%s", (key,))
            runtime.sql("UPDATE farmer_ussd_credentials SET failed_attempts=1 WHERE farmer_id=%s", (self.first["farmer_id"],))
            before, session = self.credential(), self.session(key)
            original = repository._UssdRepository.mark_verified
            def expire_at_write(store, *args):
                cursor = store._cursor
                class FinalWrite:
                    def __getattr__(self, name):
                        return getattr(cursor, name)
                    def execute(self, sql, params=()):
                        if sql.startswith("UPDATE ussd_sessions SET state = 'verified'"):
                            cursor.execute("UPDATE ussd_sessions SET expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 MICROSECOND WHERE session_key=%s", (key,))
                        return cursor.execute(sql, params)
                store._cursor = FinalWrite()
                try:
                    return original(store, *args)
                finally:
                    store._cursor = cursor
            with patch.object(repository._UssdRepository, "mark_verified", expire_at_write):
                self.assertFalse(self.authenticate(sid)["authenticated"])
            self.assertTrue(self.credential() == before and self.session(key) == session, "Rejected transition committed changes.")
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM ussd_callback_results WHERE session_key=%s", (key,))[0]["n"], 2)

        def test_duplicate_registration_race_has_one_linked_account(self):
            phone = "+2547" + f"{secrets.randbelow(100000000):08d}"
            payloads = [self.payload(phone), self.payload(phone)]
            barrier = Barrier(2)
            original = registration.phone_number_exists
            def checked(cursor, number):
                found = original(cursor, number)
                barrier.wait(timeout=15)
                return found
            def attempt(data):
                with runtime.app.app_context():
                    return registration.register_farmer(data)
            with patch.object(registration, "phone_number_exists", checked), ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(attempt, data) for data in payloads]
                results = [job.result(timeout=30) for job in jobs]
            self.assertEqual(sum(r["ok"] for r in results), 1)
            self.assertEqual([r["code"] for r in results if not r["ok"]], ["duplicate_phone"])
            self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM users u JOIN farmers f ON f.user_id=u.user_id WHERE u.phone_number=%s", (phone,))[0]["n"], 1)

        def test_concurrent_pin_changes_increment_versions_without_lost_writes(self):
            self.assertEqual(self.pin().status_code, 303)
            _, key = self.start_pin()
            before = self.credential()["pin_version"]
            barrier, ready = Barrier(2), Event()
            original = pin_management.hash_ussd_pin
            # Synchronize after real hashing has released the unchanged scrypt gate.
            def hashed(*args):
                encoded = original(*args)
                ready.set()
                barrier.wait(timeout=15)
                return encoded
            def attempt(pin):
                with runtime.app.app_context():
                    return pin_management.save_farmer_ussd_pin(self.first["user_id"], self.first["farmer_id"], self.password, pin, pin)
            with patch.object(pin_management, "hash_ussd_pin", hashed), ThreadPoolExecutor(max_workers=2) as pool:
                # Web-password checks also share the gate; sequence admission so
                # this case tests database writes rather than capacity rejection.
                first = pool.submit(attempt, "0000")
                self.assertTrue(ready.wait(timeout=15), "First real hashing phase did not reach synchronization.")
                second = pool.submit(attempt, "1234")
                results = [first.result(timeout=30), second.result(timeout=30)]
            self.assertTrue(all(r["ok"] for r in results))
            row = self.credential()
            self.assertEqual(row["pin_version"], before + 2)
            self.assertEqual(sum(verify_ussd_pin(self.first["farmer_id"], pin, row["pin_hash"], row["pin_key_version"]) for pin in ("0000", "1234")), 1)
            self.assertEqual(self.session(key)["state"], "ended")

        def test_pin_reset_during_verification_prevents_stale_authentication(self):
            self.assertEqual(self.pin().status_code, 303)
            sid, key = self.start_pin()
            ready, release = Event(), Event()
            original = authentication.verify_ussd_pin
            def verified(*args):
                result = original(*args)
                ready.set()
                if not release.wait(timeout=15):
                    raise RuntimeError("Synthetic synchronization timeout")
                return result
            def attempt():
                with runtime.app.app_context():
                    return self.authenticate(sid)
            with patch.object(authentication, "verify_ussd_pin", verified), ThreadPoolExecutor(max_workers=1) as pool:
                job = pool.submit(attempt)
                try:
                    self.assertTrue(ready.wait(timeout=15))
                    self.assertEqual(self.pin(pin="0000").status_code, 303)
                finally:
                    release.set()
                self.assertFalse(job.result(timeout=30)["authenticated"])
            self.assertEqual(self.session(key)["state"], "ended")
            self.assertEqual(self.credential()["failed_attempts"], 0)

        def test_concurrent_partial_plot_updates_preserve_both_fields(self):
            plot_id = self.plot().json["plot_id"]
            barrier = Barrier(2)
            def change(data):
                with runtime.app.app_context():
                    barrier.wait(timeout=15)
                    return update_farmer_plot(self.first["farmer_id"], plot_id, data)
            with ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(change, data) for data in ({"plot_name": "Concurrent name"}, {"location": "Concurrent landmark"})]
                self.assertTrue(all(job.result(timeout=30)["ok"] for job in jobs))
            self.assertEqual(self.rows("SELECT plot_name,location FROM farm_plots WHERE plot_id=%s", (plot_id,))[0], dict(plot_name="Concurrent name", location="Concurrent landmark"))

        def test_utc_authentication_timestamps_across_session_timezones(self):
            self.assertEqual(self.pin().status_code, 303)
            for zone in ("+00:00", "+03:00"):
                with runtime.timezone(zone):
                    low = self.rows("SELECT UTC_TIMESTAMP(6) AS stamp")[0]["stamp"]
                    self.assertTrue(abs((low.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds()) < 5, "Application/database UTC clocks differ.")
                    sid, key = self.start_pin()
                    self.assertTrue(self.authenticate(sid)["authenticated"])
                    high = self.rows("SELECT UTC_TIMESTAMP(6) AS stamp")[0]["stamp"]
                    row = self.session(key)
                    self.assertTrue(low <= row["authenticated_at"] <= high and row["created_at"] <= row["authenticated_at"] < row["expires_at"])
                    runtime.sql("UPDATE ussd_sessions SET created_at=UTC_TIMESTAMP(6)-INTERVAL 10 MINUTE,expires_at=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE session_key=%s", (key,))
                    self.assertFalse(self.authenticate(sid)["authenticated"])

        def test_non_strict_mode_exact_readback_and_warning_observation(self):
            long_name = "🌽" * 100
            response = self.plot(plot_name=long_name, area_acres="999999.999")
            self.assertEqual(response.status_code, 201)
            row = self.rows("SELECT plot_name,area_acres FROM farm_plots WHERE plot_id=%s", (response.json["plot_id"],))[0]
            self.assertTrue(row == dict(plot_name=long_name, area_acres=Decimal("999999.999000")))
            result = runtime.sql("UPDATE farm_plots SET location=%s WHERE plot_id=%s", ("Synthetic exact readback", response.json["plot_id"]))
            self.assertEqual(result["warnings"], 0)
            # Deliberately bypass application validation on this case's synthetic
            # row to document non-strict ENUM coercion, not to endorse it.
            probe = runtime.sql("UPDATE users SET role='invalid-synthetic-role' WHERE user_id=%s", (self.second["user_id"],))
            self.assertGreater(probe["warnings"], 0)
            self.assertEqual(self.rows("SELECT role FROM users WHERE user_id=%s", (self.second["user_id"],))[0]["role"], "")

    return unittest.defaultTestLoader.loadTestsFromTestCase(MariaDbFeatureTests)
