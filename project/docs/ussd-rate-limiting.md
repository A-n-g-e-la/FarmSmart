# Single-process prototype USSD rate limiting

This optional layer protects `POST /ussd`, including the public legacy menu,
without enabling persistent authentication. It uses only Python's standard
library and does not access MariaDB or hash PINs. It is disabled by default.

## Controlled demonstration configuration

Set these environment variables **before creating the Flask application**:

```powershell
$env:USSD_PERSISTENT_FLOW_ENABLED = "false"
$env:USSD_RATE_LIMITING_ENABLED = "true"
$env:USSD_GLOBAL_CALLBACKS_PER_SECOND = "10"
$env:USSD_GLOBAL_CALLBACK_BURST = "10"
$env:USSD_PHONE_CALLBACKS_PER_MINUTE = "20"
$env:USSD_PHONE_CALLBACK_BURST = "5"
$env:USSD_NEW_SESSIONS_PER_MINUTE = "3"
$env:USSD_RATE_LIMIT_MAX_KEYS = "4096"
```

The numeric values shown are defaults. Limits must be positive integers, with
bursts no greater than their corresponding counts. Initial requests must not
exceed the phone count. Counts are capped at 10,000; tracked-key capacity must
be between 3 and 20,000 to accommodate a welcome request's three quota entries.
The feature flag accepts `true`/`false` case-insensitively; other values fail
closed when a callback arrives. Invalid settings or a missing adapter return
503, without disabling unrelated web pages. Do not change policies during a
running demonstration; restart the application to apply configuration changes.

Use exactly **one serving process and one application instance**, with no
reloader or overlapping server. Threads share that application's limiter and
its lock. These settings do not start a service, enable PIN authentication,
change database configuration, or make the ordinary development launcher
database-disabled. Reuse a separately approved database-blocked launcher for
public legacy connectivity tests. Debugging and body-capturing instrumentation
must remain disabled during those tests.

## Admission behavior

Global admission occurs before request-body parsing, including malformed
requests. After the unchanged size, content-type and field validation, the
normalized phone quota is checked. Empty `text` also checks the initial-request
quota. The phone and initial quotas are checked together under one lock; denial
consumes neither. Global admission already consumed is not refunded.

Each quota enforces both an exact sliding window and a token bucket refilling at
`count / seconds`. The global window is one second; phone/initial windows are
60 seconds. At an exact window boundary, timestamps outside the window expire.
The phone burst allows five immediate steps; it then refills one token every
three seconds at the defaults. Initial requests allow three immediate starts,
but no more than three in a sliding minute. Provider retries count too: repeating
the same callback or changing its session ID does not reset quotas.

Denial returns HTTP 429 and a fixed plain-text `END` message. Capacity exhaustion,
clock/configuration failures and unexpected limiter errors return HTTP 503 with
the same safe message. Cache-Control remains `no-store`. Rejected requests do
not reach persistence, PIN verification or attempt updates. No waiting/retry
queue is introduced. Under normal load, all existing legacy menu responses are
unchanged; disabling this feature restores the original unthrottled behavior.

## Memory and security boundaries

Counter keys contain keyed normalized-phone fingerprints, never raw phone or
session identifiers. The random HMAC key exists only in memory, solely for these
volatile counters; it is not a PIN pepper or a persistent metadata key and is
never written or logged. Each accepted window retains at most its configured
count of timestamps. Expired entries are reclaimed during admission. The key
limit includes the global entry and separate phone/initial entries. If full,
new entries are refused without evicting active quotas. Idle state remains
bounded even when no requests arrive. Cleanup scans at most the configured key
capacity per call; this is suitable for a small prototype, not a flood defence.

Phone numbers and session IDs are untrusted input. Forging another phone can
consume its quota; rotating phones can evade individual quotas but not the
global limit. This feature neither authenticates Africa's Talking nor proves
phone ownership. Provider-origin verification is deferred as a documented
prototype limitation. Public persistent authentication must stay disabled.

The limiter deliberately declares `deployment_wide = False` and does not
implement `SharedCallbackLimiter`. The committed persistent path still requires
its independent shared adapter, relay identity, TLS and secrets. The local layer
does not bypass or replace those checks. Private mocked authentication tests
exercise both layers without representing provider verification as complete.

Restarts erase counters. Separate app instances, Flask workers or hosts each
have separate budgets, so effective limits multiply. Before deploying multiple
workers/instances, replace local-only enforcement with a genuinely shared
backend (for example Redis) and validate concurrency/outage behavior. No Redis,
package installation, schema change or migration is required for this feature.

Tests use fake clocks, synthetic identifiers, mocked connectors and bounded
thread pools. They verify navigation, all quotas, normalized-phone/session
bypass attempts, atomic denial, expiry, bounded state, safe failures, and
coexistence with PIN lockouts/session replay. No live callbacks or database
connections are required.
