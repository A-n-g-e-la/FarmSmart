# Stage 2C.1: separate USSD PIN cryptography

This module provides `hash_ussd_pin(farmer_id, pin)` and
`verify_ussd_pin(farmer_id, pin, pin_hash, pin_key_version)`. Call inside a Flask
application context. It opens no connection, writes no records, changes no
routes, and implements no enrollment, account authorization, attempt counter,
lockout, plot, or SMS workflow. Existing web-password authentication is unchanged.

## Compatibility and algorithm

Inspection of the installed Werkzeug implementation and Stage 2B repository
confirmed compatibility before implementation. New hashes use the existing
`scrypt:N:r:p$salt$128-lowercase-hex-digits` envelope, fit `VARCHAR(255)`, and
pass `replace_pin_hash`'s format validation. No schema or migration change is
required. This helper does not call that repository method itself.

Only four ASCII digits supplied as a string are accepted. No integer conversion,
trimming, or Unicode-digit normalization occurs; `0042` and `0000` are valid.
HMAC-SHA256 uses a dedicated pepper and a domain tag, an unsigned 8-byte farmer
ID, an unsigned 2-byte pepper version, and the exact ASCII PIN. Its hexadecimal
result is passed to Werkzeug scrypt with `N=131072`, `r=8`, `p=1`, and a fresh
32-character cryptographically random alphanumeric salt for each hash. The salt
is public; the pepper is not persisted with the hash. Farmer/version binding
prevents copying a credential unchanged to another farmer or version.

The explicit work factor follows
[OWASP's scrypt recommendation](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Werkzeug supplies salt generation and constant-time digest comparison:
[Werkzeug security helpers](https://werkzeug.palletsprojects.com/en/stable/utils/#werkzeug.security.generate_password_hash).
Each operation uses approximately 128 MiB for scrypt; benchmark concurrency and
latency on deployment hardware before exposing it through USSD.

Verification accepts only this stage's exact work factor, 32-character salt,
and digest format before invoking scrypt. It rejects unsupported/hostile work
factors rather than letting stored parameters request unbounded resources.
Stage 2B synthetic hashes are not enrolled credentials. Introducing another
real hash policy later requires an explicit bounded compatibility policy.

## External environment configuration

Supply these through protected deployment environment configuration. Never
commit real values to `.env.example`, source code, logs, or test fixtures:

- `USSD_PIN_PEPPERS`: a JSON object mapping canonical positive decimal version
  strings to canonical standard-base64 encodings of independently generated
  32-byte secrets. Versions range from 1 through 65535. Duplicate version keys,
  duplicate pepper values, malformed encodings, and oversized key rings fail.
- `USSD_PIN_CURRENT_KEY_VERSION`: the version to use for new hashes. It must
  exist in the key ring. No default is supplied.
- `USSD_METADATA_HMAC_KEY`: when provisioned, the separate metadata key. Its
  raw/encoded value is compared against every pepper, as is Flask `SECRET_KEY`.
  A repeated value causes PIN configuration to fail. This setting does not wire
  metadata helpers to live routes. If absent, no metadata key is created; ensure
  independent provisioning before future metadata integration.

The application config reads the environment values without generating secrets.
PIN configuration is validated when the PIN helpers are called, so missing PIN
configuration does not disrupt existing web login or Stage 1 menus. Validation
requires a configured key ring; it never reads database credentials or falls
back to Flask's key. A format check cannot establish randomness: operators must
provision strong independent secrets. No production secrets were generated in
this implementation or its tests.

## Pepper rotation and errors

`hash_ussd_pin` returns only `pin_hash` and `pin_key_version`. These are internal
storage values, not an HTTP/API response. Never return them to a farmer.
Persist the returned version unchanged alongside its hash. The existing
`pin_version` column is a separate credential-revocation counter, not a pepper
version; Stage 2B increments it during replacement.

For rotation, add a new independent pepper under a new version, retain old
entries, then change the current version. Existing hashes continue verifying
with their stored version. Never replace the bytes under an existing version.
The helper cannot detect a secret-manager operator overwriting an existing key.
Remove an old key only after all credentials referencing it have been migrated
or reset through a separately authorized workflow. Migration requires the PIN
on successful verification or a reset; hashes cannot be re-peppered alone.
No automatic rotation, rehash, database write, or credential invalidation occurs.

Incorrect PINs, invalid PIN formats, malformed hashes, and invalid stored version
types return `False` without detailed credential errors. Missing keys (including
a stored version removed from the ring) raise `UssdPinConfigurationError` with a
fixed message. Other cryptographic/runtime failures raise `UssdPinError` with a
fixed message and suppressed diagnostic chaining. Future callers must treat
these exceptions as operational failures, not failed PIN attempts. Intentional
hash-input validation raises `UssdPinValidationError` with fixed safe messages.

The module logs nothing. Raw PINs, HMAC material, hashes, and keys necessarily
exist transiently in process memory; disable request-body/local-variable capture
in debugging and error reporting. Python does not guarantee memory zeroization.
These helpers do not equalize account lookup timing: unknown-account dummy work,
active-status checks, callback-origin verification, expiry, lockout, duplicate
handling, and revalidated plot ownership remain future integration work. A
four-digit PIN still needs strict online attempt limits before live use.
