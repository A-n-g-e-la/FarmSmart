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
