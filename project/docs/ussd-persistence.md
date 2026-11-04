# Stage 2B: USSD persistence contract

This document describes the Stage 2B storage layer. The Stage 1 callback does not
import or call this repository. Stage 2C.1 adds standalone PIN cryptography;
see [PIN cryptography](ussd-pin-cryptography.md). PIN enrollment, live verification,
lockout decisions, account authorization, plot selection, predictions, and SMS
remain unimplemented.

## Storage and secrets

- `farmer_ussd_credentials`: one row per existing farmer; a null `pin_hash` and
  version zero mean unenrolled. Rows are created on demand, not backfilled.
- `ussd_sessions`: keyed session identifier, keyed phone tag, exact configured
  service code, optional candidate farmer, state, verification metadata, and
  absolute UTC expiry/retention. No raw provider ID or phone is stored.
- `ussd_callback_results`: one accepted fingerprint and catalog response per
  session/input position. It contains no input history or PIN.

`ussd_fingerprints.py` uses domain-separated, length-framed HMAC-SHA256 for the
session, normalized phone, and session/phone-bound cumulative input. The caller
supplies a stable metadata key of at least 32 bytes, held outside the database
and repository. In Stage 2C, configure that key separately from both Flask's
`SECRET_KEY` and the PIN-pepper secret. There is no fallback or key generation.
Do not pass a bare SHA digest or encode raw identifiers as 32-byte values.

The helpers necessarily receive raw values transiently. Never log their
arguments, retain the cumulative input, or attach request bodies/locals to error
reports. Test keys and the syntactically encoded hash fixture are synthetic;
no PIN hashing or production-secret generation is performed by these modules.
Metadata-key rotation requires expiring old sessions; a key-ring migration is
not implemented. PIN pepper version storage is ready for Stage 2C.

## Transaction API

Only acquire storage through `with ussd_transaction() as store:` inside the
configured Flask application context. It opens one connector connection, starts
an explicit transaction, and uses a dictionary cursor. No repository method
commits or opens its own connection. Returning from the block commits once.
Any failure rolls back; even a caught method error poisons the transaction and
causes an error at exit. Closed repository objects cannot be reused. Cleanup
attempts both cursor and connection even if one close fails.

Intentional validation failures use `UssdValidationError` (a `ValueError`
subclass for compatibility), with fixed, non-sensitive messages. Only this
explicit validation type and the safe persistence exception types pass through
unchanged. Unexpected `ValueError` and other operational exceptions from
connection helpers, cursors, or the transaction body become fixed
`UssdPersistenceError` messages, with diagnostic exception chaining suppressed.
Trusted validation errors also poison the unit if caught inside the block.

Public methods:

| Operation | Contract |
| --- | --- |
| `get_credentials` | Locked credential read, including both channels' counters/lockouts. |
| `ensure_credentials` | Idempotent unenrolled-row creation without changing an existing credential. |
| `replace_pin_hash` | Accept an encoded scrypt hash and pepper version; increment PIN version, clear counters/lockouts, and end farmer sessions in the same transaction. |
| `set_attempt_state` | Persist a caller-computed count and lockout for USSD or web reverification. Does not decide whether an attempt failed or a lockout expired. |
| `create_session`, `get_session` | Insert once or retrieve by derived key; never overwrite a duplicate session. |
| `set_session_state` | Enter PIN state from a live welcome session, or end a session and clear verification metadata. |
| `mark_verified` | Store a matching enrolled credential version for the session's candidate farmer; refuse wrong candidate, version, state, or expiry. Not a PIN or account verifier. |
| `end_farmer_sessions` | Lock the farmer credential then end that farmer's sessions and clear verification metadata. |
| `get_callback_result` | Locked lookup by session and input position. A result is not an authorization grant. |
| `store_callback_result` | Insert a keyed fingerprint and fixed catalog response, and advance the accepted input position atomically. |
| `cleanup_retained_sessions` | Bounded deletion only when both expiry and retention have elapsed; results cascade with their parent. |

`session_expired(record, now)` is a pure check: ended or `now >= expires_at`.
All supplied times must be timezone-aware. Repository parameters are converted
to naive UTC for MariaDB `DATETIME(6)`; values returned by the connector are
interpreted as UTC. State changes do not extend expiry or retention.

Counts are bounded by schema/API to three USSD and five web failures. These
bounds reserve the Stage 2A policies, but this layer does not implement lockout
timing or password/PIN checks. Stage 2C must enforce counter/timestamp consistency.

## Multi-step authentication and retries

Future service lock order: existing user/profile authorization locks where
needed, then farmer credentials, session, callback result. Use the same order
for reset, verification, and deactivation. Lock credentials before computing a
new count; never calculate counters from an earlier unlocked read.

Within that single transaction, the future service must:

1. Revalidate active farmer identity and acquire the credential lock.
2. Load and lock the session; check the phone tag, configured service code,
   expiry, candidate identity, credential version, and current lockout.
3. Look up the input position before applying attempt effects. An exact
   fingerprint duplicate returns its prior safe result without repeating work.
   Recheck revocation before returning any protected result.
4. For a new position, verify the previous prefix fingerprint and expected
   transition. Reject changed, skipped, or mismatched histories.
5. Perform Stage 2C verification and persist counter/state changes, then store
   the callback result before leaving the transaction. For a terminal response,
   store the result and advance its input position **before** calling
   `set_session_state(..., "ended", ...)`, within this same transaction. A failure
   in either operation rolls back the whole unit.

The initial empty callback has position zero. `last_input_count = -1` means no
accepted result yet; every new insertion advances exactly one position, through
the existing callback limit of 16. Session lifetime is checked on new results.
New results for an ended session are refused, including sessions invalidated by
PIN replacement; no new result or input-position advancement is permitted.
Ending the session before storing its terminal result is therefore refused.

An exact duplicate detected by `store_callback_result` returns `False` and makes
the entire unit rollback-only, protecting against earlier accidental counter
updates. No subsequent operations are permitted in that unit. A differing
fingerprint/response/status at an occupied position raises `UssdConflictError`;
no overwrite is performed. Concurrent insert collisions roll back and require
the caller to retry a fresh read, not to overwrite. A previously stored result
does not itself prove the session is still live or authorized. Exact duplicates
remain recognizable after ending or expiry, but recognition only rolls back
the unit: it cannot authorize new work or bypass revocation checks. Conflicting
duplicates are still rejected even when the session has ended.

Only fixed messages from `SAFE_RESPONSES` are accepted, via a symbolic response
code. Arbitrary response strings, including PIN-bearing text, are rejected
before SQL. Dynamic plot responses need a separately reviewed safe formatter
in a later stage. All session-key, phone-tag, and input-fingerprint storage
parameters must be 32-byte values from the HMAC helpers.

Database errors are replaced with fixed exceptions; SQL diagnostics and parameter
values are never logged. A commit failure has an uncertain outcome: reconcile
the callback key or credential version through a new transaction before retrying.
Do not claim a rollback necessarily undid a lost commit acknowledgement.

## Limits and pending decisions

- Account eligibility, actual PIN verification, prefix validation, callback
  origin verification, configured-code matching, and lockout rules belong to
  Stage 2C/services. Repository access must never be exposed directly as an HTTP
  operation. No current routes call this layer.
- Session expiry and retention durations are explicit caller inputs. The
  provider retry horizon and cleanup schedule still need approval. Retain replay
  records long enough; deletion removes the ability to recognize old keys.
- A session candidate can be provided on creation. Deferred phone lookup and
  candidate binding can be designed when the authentication flow is implemented.
- Mocked tests establish SQL/transaction intent, not real MariaDB constraint,
  DDL, or concurrency behavior. No database was used for Stage 2B.
- No cleanup scheduler or SMS deduplication/outbox is included. The cleanup
  function only affects the new session/result tables.

See `schema/migrations/README.md` for the separately approved manual migration.
