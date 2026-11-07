# Opt-in MariaDB integration tests — Phase 6A

**Prepared only. No database-backed tests were run during implementation.**
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

Use a fresh Python process. The runner blocks dotenv before application imports,
installs a connector guard first, and overrides Flask configuration explicitly.
Flask, metadata-HMAC and PIN-pepper keys are independent ephemeral synthetic keys;
no production/application secrets are loaded or generated persistently.
`TESTING=True`, `DEBUG=False`, and `USSD_PERSISTENT_FLOW_ENABLED=False` are forced.

Every connection validates its exact arguments and loopback resolution before
connecting, then selected database, server version, port, SQL mode, isolation,
constraint enforcement and account privileges. Unexpected targets/options are
rejected, resources closed, and diagnostics sanitized. Connect timeout is five
seconds; socket read/write timeouts are ten seconds. These are not server-wide
query/lock timeouts. The schema-scoped account supplies a database-enforced
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
All 20 cases are executable unittest methods and collection asserts that exact
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
