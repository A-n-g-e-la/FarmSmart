# USSD repository performance safety

Only the USSD repository uses `get_ussd_db_connection()` in `app/db.py`. Its
existing import alias remains `get_db_connection` for repository callers/tests.
Callback authentication and dashboard PIN management therefore share these
protections. The general `get_db_connection()` and unrelated registration,
login, officer and plot services retain their existing behavior.

## Configuration

| Environment setting | Default seconds | Scope |
| --- | ---: | --- |
| `USSD_DB_CONNECTION_TIMEOUT` | 3 | Connector connection establishment |
| `USSD_DB_READ_TIMEOUT` | 5 | Connector socket reads |
| `USSD_DB_WRITE_TIMEOUT` | 5 | Connector socket writes |
| `USSD_DB_LOCK_WAIT_TIMEOUT` | 2 | Session InnoDB row/table lock waits |
| `USSD_DB_STATEMENT_TIMEOUT` | 3 | Session statement execution |

All five values are validated before connecting: integer values or canonical
positive decimal environment strings, from 1 to 30 seconds inclusive. Missing
values, zero, negatives, booleans, floats, whitespace and malformed strings fail
closed. The limits apply independently, not as one overall request budget.
Configuration is read before every new repository connection. No credentials,
diagnostics or submitted values are returned in errors or written to logs.

After connecting with finite socket timeouts, the helper uses a temporary cursor
to execute these parameterized statements, then closes that cursor:

```sql
SET SESSION innodb_lock_wait_timeout = %s;
SET SESSION max_statement_time = %s;
```

Only after successful initialization does the repository start its transaction.
Initialization failures attempt rollback and close the cursor/connection; no
unprotected connection is returned. There is no global setting change, schema
change, reconnect loop or automatic retry of authentication writes.

## Compatibility evidence and limits

The installed Connector/Python 26.7.0 implementation accepts `connection_timeout`,
`read_timeout` and `write_timeout`, including in its C-extension connection path.
The official [connector argument reference](https://dev.mysql.com/doc/connector-python/en/connector-python-connectargs.html)
documents socket read/write timeouts (introduced in 9.2.0). They must be supplied
at connection creation; the C extension does not support changing them afterward.

MariaDB documents session-scoped
[InnoDB lock wait limits](https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables#innodb_lock_wait_timeout)
and [statement execution limits](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/query-optimizations/aborting-statements).
These settings are intended for the previously identified MariaDB 10.4.28 server.
No database was contacted for this feature: actual statement acceptance,
permissions, elapsed timeout behavior and connector/server interaction still
require a separately approved isolated-database test. Unsupported settings fail
closed rather than falling back to unbounded execution.

Statement abortion is cooperative and may exceed its requested duration. Socket
timeouts are per operation, not a hard callback deadline; transactions include
multiple operations and authentication includes two transactions and hashing.
DNS, OS scheduling, connector internals, metadata locks and cleanup can introduce
additional delays. Do not claim these settings guarantee a total response time
or immediate server-side cancellation. InnoDB lock timeout ordinarily rolls back
only the failing statement; the repository still attempts full rollback.

A read timeout during commit can mean the server committed but its reply was
lost. A subsequent rollback does not prove the commit was undone. The public
operation fails safely; existing callback reconciliation/replay checks remain
responsible for recognition of stored outcomes. No retry of an authentication
write is introduced. Cleanup warnings use fixed operation labels only.

The non-waiting, one-operation-per-process scrypt gate, hash parameters, PIN
attempt policies, session expiry/UTC guards, replay checks, rate limiter and
persistent ingress requirements are unchanged. Neither timeouts nor rate limits
bound all concurrent database connections. Use one serving process with low
test concurrency; broader deployment budgeting remains outside this feature.
Public persistent USSD remains disabled by default. Legacy callbacks still need
no database, PIN configuration or timeout configuration.

## Validation and later database testing

Offline tests use mocked connectors and synthetic identifiers to verify argument
validation, session setup ordering, cleanup, read/write/query/lock failures and
uncertain commit acknowledgement. They do not prove real MariaDB cancellation
or network timing. Existing USSD/authentication/rate-limiter tests are reused.

The isolated integration runner now accepts either its original exact five
identity arguments (retaining 5/10/10-second socket defaults), or those arguments
plus all three explicit socket timeouts. Explicit values must be actual integers
from 1 to 30 and are forwarded unchanged; aliases, partial sets and unrelated
options remain forbidden. All target/account/privilege/schema restrictions and
fresh-process isolation are retained. Synthetic application configuration uses
the approved USSD timeout defaults explicitly.

Five timeout scenarios are prepared alongside the original 20 integration cases.
They cover session-value readback, injected SQL failures, row-lock contention,
statement cancellation and a client read timeout. They have not been executed;
offline collection and guard tests do not certify MariaDB timing/error behavior.
See the [integration procedure](../tests/integration/README.md) for ownership,
cleanup, finite waits and the proposed read-only preflight. Obtain separate
approval for database inspection and execution; do not bypass the guard or use
development data. No application timeout or authentication policy is changed by
this integration-test extension.
