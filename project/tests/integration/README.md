# Opt-in MariaDB integration tests — Phase 6A

**Prepared only. No database-backed tests were run during implementation.**
The timeout extension now prepares 25 scenarios (the original 20 plus five).
Earlier results for 20 scenarios do not validate the added timeout behavior.
The extended suite must not run until separately approved database preflight
and execution authorization have been obtained.
These tests create and delete synthetic rows, using real application services,
repositories, Flask test clients, InnoDB transactions and PIN hashing. They do
not need Flask/ngrok servers, provider access, SMS, new packages, or persistent
USSD. They must never use development data or its backup.

## Provisioning requires separate approval

After approval, an operator must create a new, lower-case, uniquely named schema
on localhost:3306 / MariaDB 10.4.28, for example
`farmadvis_integration_20261107_<8-to-16-lowercase-alphanumeric-suffix>`.
Record its exact approved name. Execute the current `schema/schema.sql` only in
that new database; it already contains the final seven-table schema. Do not
execute migrations or schema.sql against an existing installation.

Create a dedicated local account named `farmadvis_it_<4-to-16-character-suffix>`.
Grant only SELECT, INSERT, UPDATE and DELETE on the exact integration schema,
without GRANT OPTION, global privileges, roles, or extra table/column grants.
Escape underscores in the database grant pattern so they are literal characters.
Unescaped underscore patterns are rejected, even when their spelling matches the
database name: they also authorize other matching schema names. MariaDB 10.4.28
exposes the stored grant pattern in `SCHEMA_PRIVILEGES`; its current-account
privilege metadata can be read without granting access to the `mysql` tables.
The setup account is not used by the runner. Do not reuse development credentials.
Provision the account password through a protected channel; never put it in Git,
command-line arguments, screenshots, transcripts, or an example file.

Preserve `farmsmart`, `farmsmart_test`, the migration rehearsal database and the
restore-verification database. The runner creates/drops no databases or users.

## Dedicated configuration and execution

All settings below are mandatory. Set them in the test process environment,
not the development `.env`. Missing or malformed configuration fails before
connecting; the runner never falls back to MYSQL_* settings.

| Setting | Value |
| --- | --- |
| `RUN_MARIADB_INTEGRATION_TESTS` | Literal `1` |
| `TEST_MYSQL_HOST` | `localhost` or `127.0.0.1` |
| `TEST_MYSQL_PORT` | Literal `3306` |
| `TEST_MYSQL_DATABASE` | The exact approved integration schema |
| `TEST_MYSQL_APPROVED_DATABASE` | The same exact name, explicitly acknowledged |
| `TEST_MYSQL_USER` | The dedicated, schema-scoped account |
| `TEST_MYSQL_PASSWORD` | Supplied securely; no default |

Only after provisioning and test-execution approval, from `project`:

```powershell
.\.venv\Scripts\python.exe -B tests\integration\mariadb_runner.py
```

With separate execution approval, select exactly one scenario by its exact
method name:

```powershell
.\.venv\Scripts\python.exe -B tests\integration\mariadb_runner.py --scenario test_ussd_lock_wait_timeout_rolls_back_synthetic_update
```

No selector still runs all 25 cases. Empty, unknown, malformed, repeated or
multiple selectors are refused before entering the database runtime or opening
any connection. Allowed names are read statically from the existing suite;
module paths, expressions and wildcards are never imported or evaluated.
Single-case execution retains the same opt-in, guarded runtime, pre/post schema
and seven-table emptiness checks, unittest fixture setup and cleanup. This option
does not authorize execution or change any connection restrictions.

Use a fresh Python process. The runner blocks dotenv before application imports,
installs a connector guard first, and overrides Flask configuration explicitly.
Flask, metadata-HMAC and PIN-pepper keys are independent ephemeral synthetic keys;
no production/application secrets are loaded or generated persistently.
`TESTING=True`, `DEBUG=False`, and `USSD_PERSISTENT_FLOW_ENABLED=False` are forced.

Every connection validates its exact arguments and loopback resolution before
connecting, then selected database, server version, port, SQL mode, isolation,
constraint enforcement and account privileges. Unexpected targets/options are
rejected, resources closed, and diagnostics sanitized. The guard accepts exactly
the five identity arguments, or those same five plus all three timeout keywords:
`connection_timeout`, `read_timeout`, `write_timeout`. Explicit values must be
actual integers from 1 to 30 inclusive; partial sets, booleans, strings, aliases
and unrelated options are rejected. Explicit values are forwarded unchanged.
With no timeout keywords, fixture/general-web connections retain a five-second
connection timeout and ten-second socket read/write limits. USSD connections use
the application's 3/5/5-second defaults. These are not server-wide settings.
The schema-scoped account supplies a database-enforced
boundary as well as the Python guard. No test-side safety control makes using
an administrator account acceptable.

## Fixtures, cleanup and scope

Startup requires exactly the seven expected InnoDB tables and zero records;
non-empty databases are refused, never automatically reset. Each case uses a
unique first-name tag, random format-valid Kenyan mobile identifiers and
`example.invalid` officer emails. Numbers are identifiers only; no messages or
external requests are sent. Officer approval/assignment and legacy plot values
are synthetic fixture writes confined to this schema.

Before any scenarios, read-only metadata checks compare columns (type, nullability,
defaults, auto-increment/update behavior and collation), complete primary/unique
and secondary indexes, foreign-key targets/actions, and all CHECK expressions
against `schema/schema.sql`. The same checks run after cleanup. The small schema
reader supports the checked-in DDL grammar only; unfamiliar definitions fail
closed. Integer display widths and harmless CHECK formatting are normalized;
expression grouping and quoted spaces remain significant. A mismatch is a refusal,
not permission to repair or recreate the database.

Provisioning must use only the approved fresh schema and confirm there are no
triggers, events or concurrent writers. A DML-only account cannot reliably inspect
objects whose metadata requires additional privileges; the runner is not a sandbox
against a malicious administrator or changes made by another database client.

Services commit through separate connections, so an outer rollback is insufficient.
Case cleanup deletes only tagged users and their owned child records, including
explicitly tracked unbound USSD sessions, in foreign-key order. It does not use
TRUNCATE, DDL, disabled constraints, global configuration changes or database drops.
Cleanup failures fail the run and leave evidence for review; do not rerun against
leftover records or manually broaden cleanup without inspection/approval.
Fixture rollback/close errors are sanitized, and both resources are given a cleanup
attempt even when rollback or cursor close fails. Offline tests exercise unittest
cleanup after success, setup failure and body exceptions; interrupted processes
and uncertain network/commit outcomes can still leave synthetic rows for inspection.

Tests cover registration/login/normalization/duplicates, plot locations and
precision, legacy editing, ownership/officer visibility, CSRF and roles, PIN
enrollment/change/lockouts/versioning, scoped session invalidation, genuine
callback history, replay/expiry, rollback and synchronized races. USSD services
are called directly; persistent callback ingress is not enabled or certified by
this suite. Test wrappers inject selected failures after real SQL and coordinate
after real hashing; they do not substitute a fake SQL engine or weaken scrypt.
All 25 cases are executable unittest methods and collection asserts that exact
count without running them. Thread pools have at most two workers; barriers/events
wait at most 15 seconds and future result waits are bounded at 30 seconds. Executor
shutdown still waits for workers: these are not hard process-kill deadlines or a
substitute for investigating a stalled server. No global SQL mode/timezone changes
are made.

UTC tests set only their own connection timezones to +00:00 and +03:00. Expiry
checks retain real database UTC; no server clock is changed. Lockout expiry is
tested by moving synthetic deadlines, without 15-minute sleeps. Non-strict mode
matches development (`NO_ZERO_IN_DATE,NO_ZERO_DATE,NO_ENGINE_SUBSTITUTION`): valid
values must read back exactly; a deliberate raw ENUM probe demonstrates warnings
and coercion. Warning text, exception details, PINs and hashes are not printed.
Test failure output is intentionally sanitized. Results remain provisional until
the separately approved real run; uncertain commit/network outcomes are not
simulated as proven rollback guarantees.

## Offline isolation and remaining checks

### Prepared timeout scenarios (not executed)

Five additional cases cover:

1. Real USSD-helper connection, session values (2-second lock wait, 3-second
   statement time), a repository transaction, and unchanged fixture-session
   values on a fresh connection. No intentional delay.
2. Failure injected after actual session-initialization SQL or an attempt-state
   UPDATE, checking closed connections and unchanged synthetic credential state.
   This is fault injection, not a real network outage. No intentional delay.
3. Two separately owned connections contending for a synthetic user's row lock.
   The waiter must receive error 1205 after approximately two seconds; its prior
   synthetic update must be rolled back. This case alone uses lock-wait 2,
   statement 5 and client read 10 seconds, configured before either connection
   opens and restored even on failure. Only the contended SELECT/fetch is caught;
   setup and preliminary-update errors fail independently. SQL-only monotonic
   timing stops before rollback/close. Readback follows closure of both
   connections, before checking errno, including for unexpected SQL error codes.
   Error 1969 and socket failures do not substitute for 1205. No thread sharing
   of connections and no application-default changes.
4. Read-only `SELECT SLEEP(6)` under the default three-second statement limit;
   require MariaDB error 1969, rather than successful completion or a client error.
5. Read-only `SELECT SLEEP(6)` with test-only read timeout 1 and statement timeout
   5, so the client has time to fail first. Require a connector read-timeout or
   lost-connection error, not server error 1969. Configuration is restored even
   if initialization or assertions fail. The C extension may report 2013/2055
   rather than the pure-Python ReadTimeoutError type.

Connections and cursors are owned by each case and have `finally` rollback/close
attempts. After a socket failure the connector may already be disconnected;
rollback/cursor-close errors do not mask the original failure if connection close
succeeds. A connection-close failure always fails the case. SQL sleeps are finite
(six seconds), session statement limits remain finite, and socket waits are
bounded. Timing assertions allow scheduling tolerance up to ten seconds; they
are observations after return, not hard process-kill deadlines. These tests do
not simulate a hung kernel or guarantee an overall deadline.

The runner retains only validated diagnostic fields for the lock SELECT and
timeout-case cleanup failures: fixed phase identifier, numeric errno, five-character
SQLSTATE, allowlisted exception class and SQL-only elapsed seconds (when available).
These are printed even when a case fails; messages, SQL parameters, records and
traceback locals remain suppressed. Cleanup diagnostics never turn an unexpected
lock-case cleanup failure into a successful result.

The first extended execution passed 24 of 25 cases, but the lock-wait case
received an unretained error number other than 1205. All seven tables were empty
after cleanup. The competing-timeout separation and diagnostics above have only
offline validation until a separately approved isolated rerun; the original
failure's exact error number cannot be recovered from that report.

The existing tagged-fixture cleanup and seven-table empty-schema checks remain
mandatory. Connection-establishment failures, write timeouts and uncertain commit
acknowledgements remain mocked in offline tests. Do not stop MariaDB, alter
networking, broaden privileges or touch global settings to induce failures.
The corrected lock-wait case's MariaDB timing, error code and rollback behavior
remain unverified until separately approved isolated execution.

### Proposed read-only preflight (requires separate approval)

Load the existing dedicated TEST_MYSQL_* credentials using the established
DPAPI-protected procedure, without printing them. Do not load the development
`.env`. From `project`, the following inspects only the approved integration
target and does not import/build/run the scenario suite:

```powershell
@'
import os
import sys
sys.path.insert(0, "tests/integration")
from mariadb_runner import Settings, isolated_runtime
settings = Settings.from_environment(dict(os.environ, RUN_MARIADB_INTEGRATION_TESTS="1"))
if (settings.host != "localhost" or settings.port != 3306
        or settings.database != "farmadvis_integration_20261108_a9f19e3c"
        or settings.user != "farmadvis_it_a9f19e3c"):
    raise SystemExit("Unapproved integration target; no connection attempted.")
with isolated_runtime(settings) as runtime:
    runtime.assert_empty_schema()
print("Read-only integration identity, privileges, schema and empty-table checks passed.")
'@ | .\.venv\Scripts\python.exe -B -
```

The preflight uses SELECT metadata/count queries and closes its read
transactions; it performs no fixture DML, session-timeout SETs, schema changes or
scenario execution. It must refuse non-empty or mismatched schema state. Its
success does not prove timeout behavior and does not authorize running the suite.

This directory deliberately has no `__init__.py` and no `test*.py` modules.
Ordinary `unittest discover -s tests` excludes these database cases. The normal
offline suite includes only `test_mariadb_integration_safety.py`, which uses
mock connectors and verifies configuration, guards, failure handling, import
isolation and discovery separation.

Existing Node tests use a simulated DOM; no browser automation is added. A manual
browser smoke test, broader stress/timeout testing and real provider security
remain separate activities. Before a real run, approve database/account creation,
schema initialization, fixture writes and cleanup, test-session timezone changes,
and execution. No changes to application code or development configuration are
required by this infrastructure.

The privilege-query and CHECK metadata columns were also checked against the
[MariaDB 10.4.28 source](https://github.com/MariaDB/server/tree/mariadb-10.4.28/sql).
Actual metadata rendering, account grants, rollback and concurrency behavior must
still be validated in Phase 6B; offline mocks do not certify a live installation.
