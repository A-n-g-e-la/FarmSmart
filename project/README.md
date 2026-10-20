# Agricultural officer approval (local operators only)

Officer registration creates a pending account; it does not grant access. An
authorized operator must verify the applicant's identity, region, and authority
outside the application before approval.

From the `project` directory, with the existing environment configured for the
intended database, run in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m flask --app app:create_app approve-officer --email "officer@example.com"
```

Check the displayed normalized email, then explicitly answer `y` to approve.
Answering `n`, ending input, or omitting confirmation cancels without connecting
to the database. There is no HTTP approval endpoint or confirmation bypass flag.
The command exits nonzero on refusal or failure.

Only trusted operators should have access to the local application environment,
configuration, and database credentials. This CLI relies on OS permissions and
database access control, not a web login or an in-application administrator role.
It does not independently authenticate an operator or prove physical locality;
do not expose it through a web wrapper, untrusted automation, or shared shell.
Do not place database passwords on the command line or in shared logs. Verify
the intended database configuration before invoking the command.

Approval runs in one InnoDB transaction, locking the user and then its uniquely
linked officer profile with `SELECT ... FOR UPDATE`. It requires an officer with
`pending` status and an unset approval timestamp, conditionally changes status to
`active`, and sets `approved_at` using the database's current UTC timestamp.
Both updates must affect exactly one row before commit. Missing, farmer,
disabled, already-approved, or inconsistent accounts are refused without changes.
Concurrent approvals serialize; the second sees an already-active account.
Deadlocks, lock timeouts, and database errors are not retried automatically.

If approval cannot be confirmed (especially a connection failure during commit),
inspect the account state through an authorized read-only check before retrying.
A lost commit acknowledgement can leave a committed approval despite a CLI
error. Approval does not log the officer in; officer login is a separate feature.
No schema migration is required. No live approval was performed by automated tests.
