# Farmer USSD PIN management

The authenticated `/farmer/home#ussd-pin` dashboard section submits a standard
HTML form to `POST /farmer/ussd-pin`; JavaScript is not required for this form.
It sets or replaces a separate USSD PIN. Existing farmer web login and public
USSD menu behavior are unchanged. Persistent USSD mode remains disabled by default.

The form requires the current web password, a four-ASCII-digit PIN (including
leading zeros), confirmation and a session-bound, scoped CSRF token. The server
derives identity from the authenticated session, rejects extra/duplicate form
fields and bounds the body to 4,096 bytes. Password/PIN fields are never filled
back into HTML, stored in cookies or included in application logs/errors.
Success redirects to a GET and rotates the form's CSRF token.

`save_farmer_ussd_pin()` locks/rechecks the active farmer user and linked profile,
then reads PIN-management attempt state. It releases the transaction before
checking the web password and hashing the new PIN. Both operations use the
existing non-waiting hashing-capacity gate. A second transaction rechecks the
account, exact web-password hash and authoritative attempt state before saving.
Lock order remains user -> farmer profile -> credential -> USSD sessions.

Five incorrect web-password checks lock PIN management for 15 minutes, using the
existing `web_failed_attempts`/`web_locked_until` columns. This does not lock web
login or change the three-attempt USSD policy. Expired lockouts start a new
window. Successful PIN replacement resets both credential attempt counters.
Invalid PINs, CSRF errors, configuration/hash failures and operational failures
do not count as incorrect web passwords. No automatic write retry is performed.

The existing `hash_ussd_pin()` supplies the salted/peppered scrypt hash and key
version. `replace_pin_hash()` increments credential `pin_version` and ends all
bound live USSD sessions for that farmer, clearing their verification metadata
within the same transaction. Other farmers' and unbound sessions are unchanged.
An invalidation failure rolls back the hash/version/counter changes too.

## Before a controlled database-backed test

- Separately authorize and verify the Stage 2B schema/migration: `users`,
  `farmers`, `farmer_ussd_credentials`, `ussd_sessions`, and callback tables.
  Missing tables cause a safe failure; this feature executes no migrations.
- Provision `USSD_PIN_PEPPERS`, `USSD_PIN_CURRENT_KEY_VERSION`, and a stable
  `SECRET_KEY`; keep the metadata key separate if configured. Use synthetic
  credentials in an isolated test database. No persistent secrets are generated.
- Serve credential forms over HTTPS when accessed beyond loopback, use secure
  session cookies there, disable debug mode and request-body/APM capture.
- Existing callback-origin, relay, limiter, clock and timeout prerequisites still
  apply before enabling persistent USSD. This page does not enable that feature.

Offline tests use the real service/repository with a mocked connector. They do
not establish real InnoDB concurrency, TLS/provider security or migration status.
PIN expiry/recovery beyond authenticated web-password re-verification and
administrative plot dropdowns are outside this feature.
