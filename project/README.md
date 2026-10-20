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

# Farmer assignment (local operators only)

From the `project` directory, using the existing configured environment:

```powershell
.\.venv\Scripts\python.exe -m flask --app app:create_app assign-farmer --farmer-phone "0712345678" --officer-email "officer@example.com"
```

Verify the intended database and the identities and authority of both parties
before use. The confirmation prompt displays the normalized farmer phone number
(for example, `+254712345678`) and officer email. Explicitly answer `y` to proceed.
Cancellation or invalid input does not connect to the database. There is no
confirmation bypass flag and no HTTP assignment endpoint.

This command is for trusted local operators with protected OS access and database
credentials, under the same access boundary as `approve-officer` above. It does
not authenticate a separate administrator or prove physical locality. Never
expose it through a web wrapper, untrusted automation, or a shared shell. The
confirmation contains contact information; protect terminal logs accordingly.

One InnoDB transaction locks the farmer user and profile, followed by the officer
user and profile, using parameterized `SELECT ... FOR UPDATE` queries. Both users
must have the correct role and active status, and the officer must have an
approved linked profile. Only an unassigned farmer can be changed. The update
stores `agricultural_officers.agricultural_officer_id` in
`farmers.assigned_officer_id`, never the officer's user ID, and checks again that
the relationship is NULL. Exactly one updated row is required for commit.

An existing assignment to the same eligible officer returns success without an
update or commit. An assignment to another officer is refused; reassignment and
unassignment are not supported. Other refusals roll back and exit nonzero.
Concurrent assignments serialize on the farmer row and cannot overwrite an
existing assignment. Deadlocks, lock timeouts, and other failures are not retried
automatically. If commit acknowledgement is lost, an assignment may have committed
despite an error: inspect state using an authorized read-only check before retrying.
No schema migration is needed. Automated tests mock all database access.
