# Officer backend integration

Recovered from the existing officer registration, authentication, assignment and
dashboard branch lineage. The JSON services, password hashing, approval checks,
operator commands and most tests are reused. No ORM, dependency, frontend assets,
USSD routes or officer-assisted farmer-registration workflow is introduced.

## Demo workflow

1. GET `/officer/register` for an officer_registration CSRF token.
2. POST JSON to that endpoint with first_name, last_name, email, password,
   password_confirmation, region and optional organization. Send the token in
   X-CSRF-Token. The new officer is pending, approved_at is NULL, and no login occurs.
3. An authorized local operator runs the existing confirmation-based command:
   `.venv/Scripts/python.exe -m flask --app app:create_app approve-officer --email officer@example.test`
4. GET `/officer/login` for an officer_login token; POST email/password and token.
   Only active, approved officer profiles can log in. Failures do not disclose
   account approval or existence. Existing farmer/officer sessions are replaced
   on successful account switching. GET `/officer/home` supplies a logout token;
   POST `/officer/logout` requires that scoped token.
5. An authorized local operator assigns an existing unassigned farmer:
   `.venv/Scripts/python.exe -m flask --app app:create_app assign-farmer --farmer-phone 0712345678 --officer-email officer@example.test`

The operator commands are local-only, require confirmation and are not exposed
as HTTP endpoints. Reassignment to a different officer is refused; repeating the
same assignment is a harmless no-op. Neither command was run on development data.

## Officer read endpoints

- GET `/officer/dashboard` returns the reused assigned-farmer JSON listing.
- GET `/officer/api/farmers` lists assigned, active farmers with plot counts.
- GET `/officer/api/farmers/<farmer_id>/plots` reads their plots.
- GET `/officer/api/farmers/<farmer_id>/plans` reads their seasonal plans.
- GET `/officer/api/farmers/<farmer_id>/plots/<plot_id>/plans` filters by owned plot.
- GET `/officer/api/farmers/<farmer_id>/plans/<plan_id>` reads one plan.

All protected requests revalidate the officer's role, active status, linked
profile and non-NULL approval timestamp, using the existing officer_required
decorator. Reads bind the validated officer ID and current farmer assignment in
SQL. Client owner IDs never override authorization. Unassigned/missing resources
share 404 responses. Assigned farmers/plots without plans return empty lists.
Only allowlisted farmer/plot fields and agricultural-plan business data are
returned, including product price snapshots and a separately saved choice.
Passwords, hashes and unrelated farmer accounts are never returned.

Plots use canonical area_ha and derived acreage, with current administrative
location fields. Plan serialization reuses the agricultural-plan SQL model.
Officer reads do not call farmer mutation services, save comparisons, change
plans or clear selections. Write methods on officer read endpoints return 405;
officer-only sessions cannot use the farmer-owned mutation routes. Every officer
response is no-store. Registration/login/logout retain scoped CSRF protection
and absolute session expiry/revalidation from the recovered implementation.

## Database and remaining scope

No new database changes are required: the backend schema already includes
users.email/status/role, agricultural_officers.approved_at, and
farmers.assigned_officer_id with the appropriate relationships. Previously
prepared agricultural-plan and canonical-plot schema changes must have been
reviewed/applied before their read endpoints can work. No database schema or
development records were changed by this feature.

The dashboard here is JSON; officer HTML pages remain a frontend integration
task. Farmer registration-template/static-asset regression failures already
documented on backend remain separate. Assisted registration is deferred: any
future assisted flow must let the farmer privately choose their password;
officers do not generate farmer passwords.

Tests recover registration, approval, assignment and session suites, use mocks
and an isolated in-memory database, and block live MySQL. MySQL-specific row-lock
and transactional behavior still require a disposable MySQL verification run.
