# Plot location and farming information — Feature B

The current pilot covers **Uasin Gishu County only**. New plots have a fixed,
read-only county and require a sub-county and one of its wards. Eldoret is not
used as a substitute for an administrative sub-county. Trans Nzoia and Kitale
are not selectable. This scope does not require discarding useful training
observations from elsewhere in Kenya and does not imply independently validated
predictions for every selectable ward.

## Catalogue and validation

`app/data/plot_options.json` is the shared backend/frontend catalogue. Its six
sub-counties and 30 wards come from the [Uasin Gishu CIDP 2023–2027, Table 2,
printed page 6 / PDF page 26](https://ugcountyassembly.or.ke/participation/downloadfile/data/County%20Integrated%20Development%20Plan%20%28CIDP%29%20-%202023-2027.pdf).
Spacing around slashes is normalized; ward names are not inferred from towns.
Changing a sub-county clears the selected ward. The server independently
validates the county and complete sub-county/ward pair, including for PATCH.
The server always stores `Uasin Gishu` on creation and rejects another supplied
county; a read-only HTML control is not relied on for validation.

Existing `farming_type` stores the approved water supply choices: Rain-fed,
Irrigated, Rain-fed with supplementary irrigation, Unknown. Existing `soil_type`
stores the approved predominant texture choices: Clay, Clay loam, Loam, Sandy
loam, Sandy, Silt loam, Mixed, Unknown. These are observations supplied by the
farmer, not laboratory soil classifications. Both remain optional, with Unknown
available explicitly. `location` remains optional village/landmark text (150
characters); its old content is neither overwritten nor reinterpreted.

## Persistence and compatibility

The only new columns are nullable `county VARCHAR(50)`, `sub_county VARCHAR(100)`
and `ward VARCHAR(100)`. The fresh-install schema and prepared manual migration
003 use the same definitions. **No migration has been executed or database
accessed.** Existing installations need a separately approved application of
003 before using this release: plot reads/writes now select these columns.
See [migration prerequisites](../schema/migrations/README.md).

Old records retain NULL administrative fields and all their previous free-text
values. Editing a name, acreage or landmark does not require assigning an
administrative location. When changing that location, all three fields must be
submitted and valid together. Old unlisted soil/water text is shown as
"Previously entered" in edit controls and retained unless explicitly changed;
new or changed values must use the catalogue. No automatic county backfill is
attempted. Existing ownership checks, scoped CSRF, transactions, multiple-plot
selection and acreage handling remain in place. Officer plot details also show
the administrative fields.

## Offline verification

Existing Python registration, editing, listing, farmer-session and officer tests
exercise mocked database connections. Added cases cover all 30 selectable wards,
fixed county persistence, invalid/mismatched/malformed locations, standardized
choices and complete administrative PATCH updates. Existing Node built-in tests
cover dropdown dependencies, request payloads and preservation of legacy edits,
alongside multiple-plot, CSRF, authorization and failure handling regressions.
Neither test suite executes the SQL migration. Its MariaDB preflight and DDL
still require validation during a later, separately authorized deployment.
