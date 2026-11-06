# Farm plot acreage migration (manual, pending approval)

`001_farm_plots_area_acres.sql` changes an **empty** existing `farm_plots`
table from `area_ha DECIMAL(8,3)` to `area_acres DECIMAL(12,6) NOT NULL`,
with `chk_farm_plots_area_acres_positive CHECK (area_acres > 0)`.
It preserves all other columns, IDs, foreign keys and indexes. It contains no
record INSERT, UPDATE or DELETE statements. `schema.sql` describes fresh installs;
do not run that file against an existing database.

## Verified backup from this implementation

Before code changes, the live plot count was rechecked as zero. A full application
database dump (all four tables, plus routines/events/triggers) was created outside
the repository and restored into a new temporary MariaDB instance. Every table's
SHOW CREATE TABLE output, row count and row-content digest matched the source.
The source was rechecked after restoration; it had not changed. The temporary
server was stopped. No DDL was executed against the live database.

- Backup: `C:\Users\yiean\AppData\Local\Temp\farmadvis-acres-backup-k2_bcq7t\pre_acres.sql`
- Size: 6749 bytes
- SHA-256: `773dedddec0cfa1bafd412266338f13e11f0c20d9efd5ae0daea2458dbb5cc5d`
- Verification receipt: `verification.json` beside the backup.

The dump contains private application data. Keep it outside version control,
retain it in protected durable storage before applying DDL, and verify its hash.
If the source changes or the backup is no longer available, take and verify a new
backup. A file hash alone does not verify restorability.

## Application contract

- POST `/farmer/plots` accepts `area_acres` as ordinary decimal text (recommended),
  or an exact JSON number: 0.001 through 999999.999 inclusive, at most three
  fractional digits. Excess precision is rejected, never rounded.
- GET `/farmer/plots` returns `area_acres` as a decimal string; MariaDB returns six
  fractional digits. Dashboard totals sum millionths of an acre using BigInt.
- Storage can represent six decimal places, but farmer-entered data is deliberately
  restricted to three. Storage precision does not imply additional measurement accuracy.
- Legacy hectare input is not converted or accepted as the required acreage field.
- Deploy the changed API and frontend together. Cached old clients must reload.
- Until the live schema is migrated, the new service's plot operations will return
  safe database-unavailable responses. Do not enable the new release prematurely.

## Applying later, only after separate approval

1. Stop the application and all other plot writers. Keep them stopped until the
   migration and coordinated release are verified. The session flags below are
   operator confirmations, not proof of a backup or a mechanism for blocking writes.
2. Recheck the intended database and `SELECT COUNT(*) FROM farm_plots` using a
   read-only connection. If the count is nonzero, **stop**; do not apply this script.
3. Verify the current backup and restore procedure. Retain the previous application
   revision and original schema. MariaDB DDL implicitly commits; do not rely on
   transaction rollback for recovery.
4. Open a MariaDB client with the intended database selected. Prompt for credentials
   or use an existing protected option file; never place passwords in commands.
5. After approval and steps 1–3, set these session confirmations in that client:

   ```sql
   SET @farmadvis_acres_migration_approved = 1;
   SET @farmadvis_backup_verified = 1;
   SET @farmadvis_plot_writes_paused = 1;
   ```

6. Review and source `001_farm_plots_area_acres.sql` in the same session. Do not use
   the client's `--force` option. Its anonymous block aborts before ALTER if the
   confirmations, empty-table requirement or source column checks fail. Missing
   constraints also cause the single ALTER statement to fail.
7. Verify the resulting SHOW CREATE TABLE, zero plot count, new constraint,
   ownership/authentication behavior, and the mocked test suites. Deploy the
   matching backend and frontend before resuming service.
8. On failure, keep writers stopped and inspect the actual schema before retrying.
   This is a one-time script, not an idempotent migration. On MariaDB 10.4 do not
   assume crash-safe atomic DDL. Use the verified backup for controlled recovery;
   never overwrite newer application records blindly.

If plots appear before approval, this empty-table script is unsuitable. A separate
migration must preserve the original hectare values and calculate acres with high
precision (`area_ha / 0.40468564224`), rounding once to six decimal places after an
explicitly agreed rounding policy. It must also preserve ownership and timestamps.
Simply renaming populated hectare values would change their meaning.

MariaDB references: [anonymous blocks](https://mariadb.com/docs/server/reference/sql-statements/programmatic-compound-statements/begin-end),
[implicit commits](https://mariadb.com/docs/server/reference/sql-statements/transactions/sql-statements-that-cause-an-implicit-commit).

# USSD authentication storage: 002 (NOT EXECUTED)

`002_ussd_authentication.sql` adds only `farmer_ussd_credentials`,
`ussd_sessions`, and `ussd_callback_results`. It does not alter or backfill
`users`, `farmers`, `farm_plots`, or officer records. Existing farmers remain
unenrolled. The canonical `schema.sql` includes the identical new definitions
for fresh installations only; never run that whole file on an existing database.

Repository schema inspection found `farmers.farmer_id BIGINT UNSIGNED NOT NULL`
as its single-column primary key, with InnoDB and restrictive foreign keys.
The new references match those types and conventions. The callback-result
foreign key alone uses `ON DELETE CASCADE`, limited to results belonging to a
deleted USSD session. The migration preflights the farmer key/engine and refuses
to proceed if any of the three destination tables already exists.

Prerequisites for a later, separately approved execution:

1. Review the SQL and obtain explicit migration execution approval. Stage 2B
   authorizes file preparation only. No migration has been executed or tested
   against a real database during this stage.
2. Verify the intended MariaDB server (10.4+), database, privileges, InnoDB,
   enabled foreign-key/check-constraint enforcement, and strict SQL mode. Verify
   a current restorable backup in protected storage. Do not rely on an older
   acreage-migration backup without checking its currency and availability.
3. Pause all USSD writers and migration operators. Leave them paused until the
   tables and matching application release are verified. The variables below
   record operator confirmations; they do not pause writers or prove a backup.
4. From the repository root, open an authorized client with credentials from a
   protected option file or an interactive password prompt. For example, after
   substituting the approved host/user/database:

   ```powershell
   mariadb --abort-source-on-error --host=APPROVED_HOST --user=APPROVED_USER --password --database=APPROVED_DATABASE
   ```

5. Only after approval and verification, in that same client session:

   ```sql
   SET SESSION time_zone = '+00:00';
   SET @farmadvis_ussd_migration_approved = 1;
   SET @farmadvis_backup_verified = 1;
   SET @farmadvis_ussd_writes_paused = 1;
   SOURCE project/schema/migrations/002_ussd_authentication.sql;
   ```

6. Inspect all three `SHOW CREATE TABLE` results, constraints, indexes, and
   initial zero row counts before enabling USSD persistence. Confirm existing
   farmer/officer/plot records are unchanged using the approved verification
   procedure. Do not enroll farmers or invoke authentication as a migration test.

**MariaDB DDL implicitly commits.** The three CREATE statements are not one
atomic migration; a later failure may leave earlier tables created. Do not use
`--force`, disable constraint checks, use `CREATE OR REPLACE`, or blindly retry a
partial application. The preflight intentionally refuses reruns. Inspect actual
state and prepare a reviewed recovery/completion plan; do not drop real data.
`--abort-source-on-error` is required so a failed preflight stops the sourced
file before CREATE statements.

The migration has no dependency on the acreage migration's application: it
references only `farmers.farmer_id`. Later plot services still require their own
schema compatibility check. No schema migration is run on application startup.

Persistence API, secret handling, transaction rules, retry behavior, and pending
Stage 2C decisions: [USSD persistence contract](../../docs/ussd-persistence.md).

MariaDB client reference:
[abort-source-on-error](https://mariadb.com/docs/server/clients-and-utilities/mariadb-client/mariadb-command-line-client).

# Plot administrative location: 003 (NOT EXECUTED)

`003_plot_administrative_location.sql` adds only nullable `county VARCHAR(50)`,
`sub_county VARCHAR(100)` and `ward VARCHAR(100)` to `farm_plots`. These match
the fresh-install schema. It preserves every existing record and does not
backfill county or reinterpret the existing `location`, `soil_type` or
`farming_type` columns. New application code requires the three columns.

Execution requires separate approval, a verified current restorable backup,
paused plot writers, MariaDB 10.4+, and the existing InnoDB acreage schema
(`area_acres DECIMAL(12,6)`). Migration 002 is independent of these plot columns.
Confirm the intended database and inspect its schema first. No database was
accessed and no migration was executed during Feature B.

For a later approved operation, use an interactive authenticated MariaDB client
with `--abort-source-on-error` (never `--force`). In the same session, after
performing the checks, set `@farmadvis_plot_location_approved = 1`,
`@farmadvis_backup_verified = 1`, `@farmadvis_plot_writes_paused = 1`, then source
`project/schema/migrations/003_plot_administrative_location.sql` from the
repository root. These flags record operator confirmations; they do not perform
a backup or stop writers. The script refuses unexpected acreage schemas or any
existing administrative column.

DDL implicitly commits; this is not an idempotent migration. Inspect actual
state after any failure rather than blindly retrying. Verify SHOW CREATE TABLE,
unchanged existing data/ownership and NULL new fields on old plots, deploy the
matching application, then verify new/edited plot behavior before resuming
writers. Never execute the entire fresh-install schema on an existing database.
See [Feature B behavior and compatibility](../../docs/plot-location-dropdowns.md).
