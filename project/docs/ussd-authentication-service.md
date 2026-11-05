# Stage 2C.2A: offline USSD authentication service

Stage 2C.2B.3 connects this service through the opt-in
[callback orchestration layer](ussd-callback-foundation.md). The persistent flag
remains disabled by default; the legacy `/ussd` menu is unchanged when disabled.
Web-password login, PIN enrollment, plots, recommendations and SMS are unchanged.
No database or migration was accessed/executed during implementation or testing.

## Inspection and minimal repository extensions

The repository schema defines `users.phone_number VARCHAR(20)` with
`uq_users_phone_number`, and one farmer profile per user through
`uq_farmers_user_id`. Registration stores `normalize_kenyan_phone()` output,
`+254` plus nine ASCII national digits. Both public service entry points reuse
this normalizer; phone possession is never treated as authentication. Legacy
non-canonical numbers, if any, are not guessed or rewritten. The actual live
schema/data was not inspected; deployment must separately verify compatibility.

The committed Stage 2B repository could lock credentials and sessions, replace
a PIN/version, invalidate sessions, and atomically store callbacks. It lacked a
transaction-scoped phone lookup and deferred candidate binding. The smallest
required additions made here are:

- `get_farmer_by_phone`: parameterized, locked user lookup followed by locked
  farmer-profile lookup, fetching no web password hash.
- `bind_candidate`: bind only a live welcome session to a farmer or to no
  candidate. It cannot switch an already bound farmer or reopen an ended session.
- A fixed internal `pin_verified` result in the existing response catalog.

No schema, transaction manager, or web-authentication changes are required.
The catalog addition is not a live menu change. Caller-supplied response text
remains forbidden. Account lookup and authorization locks precede credential,
session, and callback locks. Existing PIN replacement locks credentials before
invalidating sessions, serializing it with the authentication write phase.

## Public API and session prerequisites

Call inside the configured Flask application context:

- `identify_ussd_candidate(phone_number, session_id, service_code)` processes the
  cumulative `1` step of an **existing** welcome session. Its empty-input result
  must already be stored at position zero. It binds the candidate, records the
  keyed position-one fingerprint, and enters PIN state atomically. Registered,
  inactive, and unregistered numbers receive the same generic PIN prompt if the
  welcome session has no conflicting binding. No farmer identity is returned.
- `authenticate_ussd_pin(phone_number, session_id, service_code, text)` accepts
  exactly `1*dddd`, where the PIN is four ASCII digits, including leading zeros.
  It requires an identified PIN-state session and the matching keyed prefix.
  There is one PIN submission per dial; an incorrect PIN ends that session.
- `advance_ussd_menu(phone_number, session_id, service_code, text)` cancels with
  `1*0` or validates a verified follow-up (`1*dddd*1` refresh / `1*dddd*0` exit).
  Repeated refreshes require every genuine preceding ledger entry. This method
  never hashes, counts PIN attempts or returns `authenticated=True`. It reuses
  the same binding, account/credential validation and lock ordering. Invalid
  numeric PIN shapes terminate with invalid-selection without counting a mismatch.

Successful verification stores the fixed `CON` placeholder with `0. Exit` from
the response catalog. Exact cached success has the same text but grants no new
authorization. New follow-ups revalidate current authorization and complete
history; terminal results are persisted before clearing metadata/ending the
session in one transaction. See the callback document's full state-machine table.

Identification requires a genuine position-zero welcome fingerprint, catalog
response and HTTP 200. Both PIN phases require that welcome and the matching
position-one PIN prompt with its expected response/status; missing or corrupt
history is refused before any authentication writes. Cached PIN success also
requires `created_at <= authenticated_at <= current UTC`, a valid current session
and HTTP 200. Replays still return `authenticated=False` without hashing/counting.

These methods do not create sessions or synthesize prior callbacks. Stage
2C.2B must establish the genuine welcome session and position-zero result with
the same metadata key/normalization rules. Duplicate session creation must use
the unique derived key, retry a fresh locked lookup after a collision, and never
overwrite an existing session. There is no automatic retry loop in this stage.

`USSD_SERVICE_CODE` must exactly match the configured provider code. The existing
`USSD_METADATA_HMAC_KEY` setting must contain canonical standard-base64 for an
independent 32-byte key. No default or persistent secret is generated. The PIN
crypto service additionally checks metadata/Flask key reuse against every pepper.

Public results contain only `authenticated` and fixed `response`
text. Duplicate recognition stays internal so it cannot reveal whether a
failure was stored for a registered account. Missing/inactive/unenrolled
accounts, invalid state/input, operational
failures, lockout, and capacity exhaustion all receive the same failure text.
No raw phone, farmer ID, PIN, hash, key, exception, or SQL diagnostic is returned
or logged by the service. Different processing times are not equalized; see the
mandatory ingress and enumeration controls below.

## Credential checks and attempts

Missing credential rows, null/unsupported hashes, unavailable pepper versions,
invalid credential versions, invalid counters, and inconsistent lockout fields
are operational/integrity failures. They never enter the wrong-PIN counter path.
`validate_ussd_pin_credential()` checks shape and key availability without scrypt.
Only a real boolean mismatch from `verify_ussd_pin()` for a structurally valid,
enrolled credential can increment attempts. Structural validation cannot prove
that a valid-looking digest has not been altered: arbitrary well-formed hash
corruption/tampering is indistinguishable from an incorrect PIN without a
separate integrity tag. Protect database write access; this stage adds no MAC
column or migration and does not claim to detect that case.

Three accepted incorrect attempts across dials set a 15-minute lockout. Active
lockout is checked before scrypt and again before committing. While locked,
even the correct PIN is rejected. At exactly the lockout deadline, a new dial
may verify; an incorrect PIN starts a new window at one failure, and success
clears the count/lockout. Success before lockout also resets prior failures.
Do not infer that "consecutive" resets merely because a session ended.

## Two-phase verification and races

1. In a Stage 2B transaction, lock/revalidate the phone's user/profile,
   credential, session, and callback. Check active farmer role/profile, exact
   phone/service binding, live lifetime, state, credential integrity, lockout,
   input position, and the previous cumulative-input fingerprint. Recognize an
   exact duplicate before performing any new attempt effects. Copy only the
   account identity, exact stored hash/pepper version/PIN version, and immutable
   session lifetime needed for revalidation. End this read-only transaction.
2. Call `verify_ussd_pin` outside all database locks under its shared capacity
   gate. A failure here starts no write transaction. The earlier commit, if any,
   committed reads only, not authentication or counter changes.
3. Start a new transaction and repeat all current checks. Refresh application
   decision time **after** the callback/prefix reads as well as acquiring locks,
   and recheck lifetime/lockout before writing. Calculate a new 15-minute lockout
   from this fresh decision time. Reject a changed account identity, exact
   hash, pepper version, PIN version, or session lifetime. Recompute attempts
   from the newly locked counter, not the old snapshot. Another request's
   changes therefore cannot be overwritten by an obsolete count. A newly
   active lockout defeats even an already computed successful PIN result.
4. Store counters/lockout, the accepted callback result/position, and the verified
   metadata or terminal session state together. Terminal results are inserted
   **before** marking the session ended. Any failure rolls the entire write
   transaction back. No hash or resource exception is converted into a mismatch.

The final `mark_verified` update is conditional on PIN state, the expected
farmer binding, empty verification metadata, the locked credential's expected
PIN version/enrollment/lockout, and a live session at database statement time.
It uses `created_at <= UTC_TIMESTAMP(6)` and `expires_at > UTC_TIMESTAMP(6)`;
`authenticated_at` also comes from that database UTC time. Exactly one affected
row is required. Zero rows raise a safe conflict and roll back counters, the
callback result, input position, and session metadata together; authentication
is returned only after the transaction exits successfully.

The migration and fresh-install schema use `DATETIME(6)` with a naive UTC
storage contract. The schema's `SET SESSION time_zone` only affects its own
installation connection; the application connector does not set a time zone.
[MariaDB UTC_TIMESTAMP](https://mariadb.com/docs/server/reference/sql-functions/date-time-functions/utc_timestamp)
supplies UTC independently of the session time zone, so the final guard requires
no migration or connection-setting change. Keep application and database clocks
synchronized: attempt deadlines still use application UTC. The guard checks the
final UPDATE's statement time, not later commit/response time. Future protected
operations must independently recheck session expiry and authorization; this
transition never extends the absolute session lifetime.

Concurrent identical callbacks can both acquire a read snapshot, but at most
one write-phase result is accepted. The other recognizes the existing keyed
result without incrementing attempts. Changed input at the same position and
missing/changed prefixes are refused. Duplicate recognition returns
`authenticated=False`, even for a stored successful result: it is never an
authorization grant for an additional action. Existing success metadata must
still match the active account, current credential version, lifetime, and
lockout before its safe response is replayed. PIN-reset-invalidated, expired,
or ended sessions cannot gain new authentication state.

The service never retries uncertain commits automatically. A retry uses the
stored callback identity to reconcile a possibly committed outcome. PIN resets
and deactivation must preserve the documented lock order and invalidate sessions;
phone reassignment is rechecked in the second phase. Real InnoDB deadlock,
isolation, constraint, and commit-acknowledgement behavior remains unverified.

## Resource protection and Stage 2C.2B deployment requirements

Both `hash_ussd_pin` and `verify_ussd_pin` acquire the same process-wide
`BoundedSemaphore(1)` immediately before scrypt. Admission is non-blocking, with
no waiting queue. Different Flask apps and threads in one process share it.
Capacity is released on success or exceptions. No public authentication-service
parameter can increase it or supply a verification decision. Hash parameters
remain `scrypt:131072:8:1`, approximately 128 MiB per admitted operation.

This is **not a distributed limit**: budget worker count times hash memory plus
application overhead, and benchmark callback latency. A semaphore limits work
in progress, not total guesses or sequential abuse. SQL lock waits also depend
on configured database timeouts and deployment admission limits.

Before enabling the persistent route in a deployment, require:

- Provider-confirmed callback-origin verification/source restrictions, HTTPS,
  trusted proxy configuration, and ingress/global/per-phone rate limiting.
  The enabled route authenticates a configured trusted relay and requires a
  shared-limiter adapter; the actual relay must independently verify the provider.
  The disabled Stage 1 shell remains public. A service code or supplied
  phone/session identifier is not source authentication.
- Timing/enumeration analysis: unregistered and invalid accounts perform no
  scrypt. If dummy work is introduced, put it behind the same gate and ingress
  controls. Fixed public messages alone do not establish timing resistance.
- A measured worker/memory budget and bounded request/SQL timeouts. Consider
  isolation from web traffic so authentication load cannot starve the portal.
- Secret provisioning/rotation on every worker, replay retention, initial
  session creation, authenticated menu transitions, and session-end cleanup.
- No request-body or local-variable capture in debug/APM/error reporting.
  Fixed operational metrics may be added later without personal/secret values.
- Separate approval for any migration or real-database validation. Offline
  fake-store tests model serialization and rollback; they do not certify InnoDB.

No plot retrieval, advice acceptance, SMS dispatch, or web enrollment is included.
