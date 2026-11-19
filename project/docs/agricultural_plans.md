# Agricultural plans

This feature follows the existing mysql.connector pattern; it introduces no ORM
or dependencies. Apply `schema/agricultural_plans.sql` manually only after review
and a database backup. The application does not run this SQL automatically.
Existing tables are unchanged. The script creates three InnoDB tables with
foreign keys, unique plot/year/season plans and cascading deletion of plan children.
The script was not executed against the development database.

Current backend stores plot area in `farm_plots.area_ha`. This feature reads that
value and never changes a plot. Separate plot-management branches use area_acres;
their schema changes must be reconciled before integrating those branches. No
location dropdowns or farmer/plot registration workflows are changed here.

## Farmer JSON API

All endpoints reuse farmer_required, rechecking an active farmer session, and
scope every database lookup to that farmer's existing plot ownership.
Responses use Cache-Control: no-store. Mutations require JSON and a scoped
X-CSRF-Token obtained from GET `/farmer/agricultural-plans/csrf`.

- GET/POST `/farmer/plots/<plot_id>/agricultural-plans`
- GET the same collection with `?year=2026&season=long_rains` for one season
- GET/PATCH/DELETE `/farmer/agricultural-plans/<plan_id>` (DELETE body: `{}`)
- GET `/farmer/agricultural-plans/<plan_id>/comparisons`
- PUT `/farmer/agricultural-plans/<plan_id>/selection`, body `{"scenario_id":"reduced"}`

Example POST body (quantities and all costs are for the whole plot):

```json
{
  "planting_year": 2026,
  "planting_season": "long_rains",
  "variety": "623A",
  "farmgate_price_kes_kg": 35,
  "seed_cost_kes": 2500,
  "labour_cost_kes": 4000,
  "other_cost_kes": 1500,
  "fertilizers": [{"product":"UREA","quantity_kg":50}],
  "prices": {"UREA":{"price_kes_per_bag":3500,"bag_weight_kg":50}}
}
```

Years are four-digit integers; seasons are long_rains or short_rains. Season
text is normalized for case/spaces. Updates may change variety, prices, costs
and the fertilizer list, but cannot move a plan to another plot/year/season.
All production costs are explicit; supply zero rather than omitting a cost.
Fertilizer inputs follow the completed fertilizer service, including custom
grades, per-kg prices and supplied per-product total_cost_kes. Grade percentages,
whole-plot quantities and the used price quote are preserved alongside each
calculated product cost. Unknown plan fields are rejected; no credentials are stored.

The service exposes create_plan, get_plan, list_plans, get_season_plan,
update_plan, delete_plan, compare_saved_plan and select_scenario. Each takes a
trusted authenticated farmer_id from the route, not from submitted JSON. Every
operation uses one transaction and closes its connection. Plan updates replace
products and clear any selection atomically; validation or SQL failures roll back.
Database uniqueness also protects against duplicate seasons under concurrency.

Comparisons reconstruct the original products/prices and call compare_plot_plans
with the plot's stored area. The existing yield, fertilizer and ROI formulas are
unchanged. Scenarios are returned without automatic selection or persistence.
A farmer selects by scenario ID; the server recomputes options under the plan
lock, rejects unavailable options and snapshots only the chosen products in a
separate one-row-per-plan selection table. Changing the choice never changes the
original products. Editing the plan clears the choice, including price-only edits.
An invalid edit preserves both the previous plan and previous choice.

Officer authorization/assignment helpers exist on other branches but are not
integrated in backend. No officer plan endpoint is exposed and no parallel auth
system is introduced. Officer read access must later reuse those helpers; no
officer edits are authorized by this feature. Future plot-area editing workflows
must also invalidate/revalidate affected selections.

Tests use an isolated in-memory SQLite database with a narrow connector adapter
and mocked MySQL failures. MySQL connections are blocked. They verify persistence,
rollback, foreign keys, ownership, CSRF and the real comparison integration.
MySQL-specific DDL and concurrent row-lock behavior still need verification on
a separately provisioned disposable MySQL database before deployment.

The broader regression run also found an existing frontend dependency:
test_registration_web.py has 13 missing farmer/register.html errors and one
static-asset failure. Running those tests with the unchanged backend factory
reproduces the same failures. No registration templates or assets are added by
this backend feature.
