# Frontend integration review

## Branch and reuse

Integration branch: `feature/frontend-integration`, based on backend
`789a60b08887bd328e70bfdb325c597f79a5e1d1`.
One explicitly approved merge commit, `2060353`, preserves
`farmer-authentication` (`ff04d92`) and its original registration/login history.
There were no cherry-picks, new implementation commits, pushes or merges back
to backend. Subsequent implementation remains unstaged for review.

Inspected local and corresponding remote histories: `frontend`,
`farmer-authentication`, `integration/farmer-authentication`,
`feature/farm-plot-management`, `feature/plot-location-dropdowns`,
`feature/officer-registration`, `feature/officer-authentication`,
`feature/officer-farmer-assignment`, `feature/officer-dashboard`, and
`feature/homepage` (local). Some local officer branches are ahead of their
remote-tracking counterparts. All original branches and six stashes remain.

The later branches mix frontend work with obsolete acreage schema/backend or
USSD history. Their frontend files were selectively recovered; merging their
complete histories would reintroduce unrelated work. Their commits were not
independently clean frontend units suitable for a simple cherry-pick.
Stash inspection found no additional unique required frontend workflow;
no stash was applied or popped.

## Recovered files and compatibility

All paths below are relative to `project/`.

| Source | Files reused |
| --- | --- |
| Approved farmer-authentication merge | `app/templates/farmer/{login,register}.html`, `app/static/css/{login,registration}.css`, `app/static/javascript/{login,registration}.js`, `tests/frontend/login.test.cjs` |
| feature/homepage | `app/routes/public.py`, `app/templates/public/home.html`, `app/static/css/home.css`, `tests/test_homepage.py` |
| feature/plot-location-dropdowns | `app/templates/farmer/{dashboard,register}.html`, `app/static/css/dashboard.css`, `app/static/javascript/{dashboard,farm-plots}.js`, `tests/frontend/{farm-plots,logout}.test.cjs` |
| Officer frontend present on feature/plot-location-dropdowns and earlier officer branches | `app/templates/officer/{landing,login,register}.html`, `app/static/css/officer-{auth,dashboard,registration}.css`, `app/static/javascript/officer-{auth,dashboard,registration}.js`, `tests/frontend/officer-{auth,dashboard,registration}.test.cjs`, `tests/test_officer_registration_web.py` |

Registration/login/logout validation, CSRF handling, dependent Uasin Gishu
dropdowns, assigned-farmer listing and original visual styling were reused.
The farmer register footer uses the later existing login link.

Compatibility edits register the recovered public blueprint in
`app/__init__.py`; add existing-screen delivery routes in
`app/routes/{farmer,officer}.py`; and return dashboards as HTML when HTML has
higher Accept priority than JSON. Existing JSON clients keep their contract.
Farmer dashboard placeholders and USSD controls were removed in favor of the
working plan UI. Plot JS sends `area_ha` or `area_acres` according to the selected
unit; all conversion remains in the existing backend. Editing starts from
canonical hectares. Plot deletion respects the backend's plan relationships.
Officer dashboard JS adds read-only saved plan/selection rendering.

No independent working profile editor was discovered. Existing registration,
authentication and profile ownership rules remain; no second registration or
assisted-registration API was added. Assistance uses normal registration,
private farmer password entry and separate operator assignment.

## Genuinely new files

- `app/templates/farmer/plan_editor.html`
- `app/static/css/plans.css`
- `app/static/javascript/agricultural-plans.js`
- `tests/test_frontend_integration.py`
- `tests/browser_frontend_smoke.py`
- `tests/frontend/browser-driver.js`
- This integration report.

The plan editor calls existing APIs for seasonal plans, product pricing,
comparisons and separately saved choices. It displays all returned scenarios,
yield, production, fertilizer/total costs, revenue, profit and ROI. Unavailable
scenarios have explanations and cannot be selected; no choice is automatic.
No ML, fertilizer, ROI or unit-conversion formula was duplicated in JS.

## Branch history and inspected references

Current branch: `feature/frontend-integration`, created from `backend` at
`789a60b08887bd328e70bfdb325c597f79a5e1d1` (also origin/backend at inspection).
With explicit user approval, merged `farmer-authentication` (`ff04d92`) into
the integration branch as `2060353`. The only conflict was `.gitignore`, resolved
by retaining backend's current rules. No implementation commit or cherry-pick
was made. All later implementation files remain unstaged/untracked.

Inspected frontend sources (local / remote-tracking tips where present):

| Branch | Local | Remote-tracking |
| --- | --- | --- |
| frontend | inspected via origin | bdb8a70 |
| farmer-authentication | ff04d92 | ff04d92 |
| integration/farmer-authentication | a55e311 | a55e311 |
| feature/farm-plot-management | 42d8275 | a55e311 |
| feature/plot-location-dropdowns | 6f7fcd8 | 6f7fcd8 |
| feature/officer-registration | 74254bb | 42d8275 |
| feature/officer-authentication | 9631e4b | 74254bb |
| feature/officer-farmer-assignment | cdd528a | 9631e4b |
| feature/officer-dashboard | 84ddebc | cdd528a |
| feature/homepage | 63e9993 | none inspected |

The later branches contain stacked backend/USSD history and obsolete acreage
schema changes, so full merges would regress the current backend. Their UI
changes were recovered selectively; relevant commits were not cleanly isolated
enough for a scoped cherry-pick. Six stashes remain intact; inspection found
only duplicate older registration assets, so none was applied or popped.
All listed old branches must remain for later cleanup.

## Routes and verified journeys

- `/`: original landing page and farmer/officer account choices.
- `/farmer/registration`, `/farmer/login-page`, `/farmer/home`:
  registration, authentication, plots and seasonal plans.
- `/officer/register-page`, `/officer/login-page`, `/officer/dashboard`:
  pending registration, approved authentication, assigned farmers and
  read-only plots/plans/choices.
- Existing scoped APIs continue to enforce ownership, approval, assignment,
  session expiry and CSRF. Approval and assignment remain local CLI operations.

## Database and demonstration prerequisites

No schema change is introduced and no migration/development DB write was run.
Existing backend setup, including `schema/agricultural_plans.sql` and
`schema/migrations/004_farmer_plot_integration.sql`, must already have been applied using
the backend's documented operator procedure. Canonical plot storage remains
`area_ha`. Plot-area and plan edits invalidate selections through backend logic.
Models and financial formulas are unchanged.

Before a real demonstration, the operator must confirm their MySQL setup,
approve an officer and assign a farmer through existing CLI commands, then
check the deployed browser against their actual data. Automated verification
does not certify that the development database schema or assignments are ready.
There is no USSD interface or new profile-edit screen in this feature.

## Verification

- Python unittest discovery: **332 tests passed**, zero failures/errors.
  The previous 13 missing-template errors and one static-asset failure are
  resolved. Injected failure-path logs are expected assertions, not failures.
- Recovered Node suites: **141 tests passed**, zero failures.
- Real headless Edge: farmer registration/login/logout; plot creation and edit
  in hectares/acres; plans, comparisons and explicit saved selection; reload;
  selection invalidation after area edit; unavailable options; pending officer
  registration; approved officer assigned plot/plan/choice viewing; officer
  logout. All passed using synthetic isolated SQLite fixtures. MySQL connection
  attempts were explicitly blocked. No new testing packages were installed.
- `git diff --check` passed (Git reports existing LF/CRLF normalization notices).

Earlier browser attempts identified two issues that were corrected: dashboard
content negotiation initially returned JSON for Edge's full Accept header, and
the test driver initially submitted a plot while its initial list request was
still loading. The final Edge run passed all nine grouped journeys after fixing
the route preference check and waiting for the actual plot refresh control.

The optional browser smoke harness runs from `project/` with
`.venv/Scripts/python.exe -B tests/browser_frontend_smoke.py` and requires the
installed Windows Edge browser. Its test-only driver is not a Flask app route.
It uses a temporary browser profile and starts only an isolated fixture server.

Retain all inspected old branches and preserved stashes until the user has
reviewed, committed and integrated this branch. No cleanup was performed.

## Exact final Git status

No staged implementation changes. Status after the approved merge:

```text
 M project/app/__init__.py
 M project/app/routes/farmer.py
 M project/app/routes/officer.py
 M project/app/templates/farmer/register.html
 M project/app/templates/officer/login.html
?? project/app/routes/public.py
?? project/app/static/css/dashboard.css
?? project/app/static/css/home.css
?? project/app/static/css/officer-auth.css
?? project/app/static/css/officer-dashboard.css
?? project/app/static/css/officer-registration.css
?? project/app/static/css/plans.css
?? project/app/static/javascript/agricultural-plans.js
?? project/app/static/javascript/dashboard.js
?? project/app/static/javascript/farm-plots.js
?? project/app/static/javascript/officer-auth.js
?? project/app/static/javascript/officer-dashboard.js
?? project/app/static/javascript/officer-registration.js
?? project/app/templates/farmer/dashboard.html
?? project/app/templates/farmer/plan_editor.html
?? project/app/templates/officer/landing.html
?? project/app/templates/officer/register.html
?? project/app/templates/public/
?? project/docs/frontend-integration.md
?? project/tests/browser_frontend_smoke.py
?? project/tests/frontend/browser-driver.js
?? project/tests/frontend/farm-plots.test.cjs
?? project/tests/frontend/logout.test.cjs
?? project/tests/frontend/officer-auth.test.cjs
?? project/tests/frontend/officer-dashboard.test.cjs
?? project/tests/frontend/officer-registration.test.cjs
?? project/tests/test_frontend_integration.py
?? project/tests/test_homepage.py
?? project/tests/test_officer_registration_web.py
```
