# Farmer plot integration

Reuses farm_plot_service.py, its text/location validation and protected plot
routes from feature/plot-location-dropdowns (which builds on farm-plot-management).
The checked-in plot_options.json and loader are recovered unchanged. No frontend,
USSD or officer code is imported. Registration, hashing, farmer profiles and
session authorization are unchanged. This feature uses mysql.connector, not an ORM.

## Area and schema

Store only farm_plots.area_ha, as DECIMAL(21,14). API inputs accept exactly one
of area_ha (up to 14 decimal places) or area_acres (up to 3 decimal places, as in
the earlier plot workflow). Use a positive value converting to 0.0001..10000 ha,
matching the completed fertilizer and ROI services. Acre conversion uses the
existing ACRE_TO_HA helper, interpreted as exact decimal text. Exponent strings,
non-finite values and inputs requiring silent rounding are rejected.

API outputs return decimal text for hectares and derived acreage; area_acres
is not a second stored value. The wider precision preserves e.g. 1 acre as
0.40468564224 ha instead of rounding to 0.405 ha. Model inputs remain numeric
hectares through the existing plan/comparison services.

For existing CURRENT backend databases, review and manually apply
schema/migrations/004_farmer_plot_integration.sql after backup and pausing writers.
It widens area_ha and adds nullable county/sub_county/ward, without changing existing
areas or inventing administrative labels. Fresh databases use schema/schema.sql.
No SQL migration was executed by this implementation.

For a database already changed by an earlier acres-only branch, do not apply 004
blindly or rerun the old migration that renames hectares to acres. Use a reviewed
maintenance-window conversion:

1. Verify the actual schema and a restorable backup. If BOTH area columns exist,
   stop and investigate their units/values rather than guessing the authoritative one.
2. Add a temporary nullable area_ha DECIMAL(21,14), retaining the source acres.
3. Backfill it using `area_acres * 0.40468564224`. Check all source rows have valid
   positive areas, no overflow, and exact destination equality before continuing.
   Legacy acreage with more than 3 fractional digits needs a reviewed precision
   decision before conversion; do not silently round legacy values.
4. Verify row counts, ownership, plan foreign keys and converted values against
   the backup. Make area_ha NOT NULL and retain the positive-area constraint.
5. Only after explicit review, remove the old acreage column/its old constraint
   as a controlled cutover. Never deploy two independently writable area fields.
6. Add administrative columns only if absent; preserve any existing labels.
   Revalidate/clear existing selections because this cutover changes area semantics.

These are manual steps, not an automatically executed converter. MySQL ALTER
operations implicitly commit. Existing out-of-calculation-range legacy plots must
be preserved and reviewed, not deleted or clamped.

## Protected farmer API

- GET `/farmer/plots`, `/farmer/plots/<id>`, `/farmer/plots/options`
- GET `/farmer/plots/csrf-token` for the plot_registration token
- POST `/farmer/plots` (JSON or existing form-data contract)
- PATCH `/farmer/plots/<id>` (JSON, token in X-CSRF-Token)
- DELETE `/farmer/plots/<id>` (token in X-CSRF-Token)

Example creation: `{"plot_name":"North plot","area_acres":"1.000",
"county":"Uasin Gishu","sub_county":"Soy","ward":"Ziwa",
"location":"Near the school","soil_type":"Loam","farming_type":"Rain-fed"}`.
location is the existing optional village/landmark text field, not a replacement
for administrative dropdowns. Existing ward pairs and soil/farming choices are
unchanged. County/sub-county/ward must be submitted together when updating
administrative location. No Trans Nzoia or Kitale dropdown is introduced.

Every route reuses farmer_required and every SQL lookup/update/delete scopes to
the authenticated farmer ID. Client owner IDs never grant access. Mutations reuse
CSRF protection; plot responses use no-store. Legacy plots with missing location
labels remain readable and can receive metadata-only edits.

Area updates lock the owned plot, update its canonical area and clear selections
for ALL of that plot's seasonal plans atomically. Other plot selections and the
original plan products remain unchanged. Equivalent units or metadata-only edits
do not clear choices. Comparisons subsequently read the new area from farm_plots.
Plot deletion is refused with 409 while plans reference it; farmer/plan deletion
is never cascaded by this feature. Plan creation locks the same plot, preventing
a concurrent deletion from orphaning a plan.

Tests use isolated in-memory SQLite with a connector adapter and mocked failures;
live MySQL connections are blocked. MySQL DDL/precision and concurrent locking
still require a disposable MySQL verification environment. The known backend
registration-web failures (missing farmer/register.html/static assets) remain
frontend integration dependencies and are not fixed by copying frontend files.
