"""Plot/plan integration against an isolated database, never development MySQL."""

import copy
import time
from decimal import Decimal
from unittest.mock import patch

from mysql.connector import Error

from tests.test_agricultural_plans import PlanTestBase, TestConnection, TestCursor
from app import create_app
from app.config.plot_options import PLOT_OPTIONS
from app.services import farm_plot_service as plots
from app.services import agricultural_plan_service as plans


class PlotCursor(TestCursor):
    def execute(self, sql, params=()):
        # SQLite lacks a native Decimal binding; MySQL receives Decimal directly.
        params = tuple(str(value) if isinstance(value, Decimal) else value for value in params)
        super().execute(sql, params)


class PlotConnection(TestConnection):
    def cursor(self, dictionary=False):
        return PlotCursor(self.connection)


class PlotTestBase(PlanTestBase):
    def setUp(self):
        super().setUp()
        for column, definition in (("plot_name", "TEXT DEFAULT 'Existing plot'"),
                                   ("location", "TEXT"), ("soil_type", "TEXT"), ("farming_type", "TEXT"),
                                   ("county", "TEXT"), ("sub_county", "TEXT"), ("ward", "TEXT")):
            self.db.execute("ALTER TABLE farm_plots ADD COLUMN " + column + " " + definition)
        self.connection = PlotConnection(self.db)
        for module in (plans, plots):
            connect = patch.object(module, "get_db_connection", return_value=self.connection)
            connect.start()
            self.addCleanup(connect.stop)
        self.plot_data = {"plot_name": "North plot", "area_acres": "1.000",
                          "county": "Uasin Gishu", "sub_county": "Soy", "ward": "Ziwa",
                          "location": "Near the school", "soil_type": "Loam", "farming_type": "Rain-fed"}

    def register(self, farmer=91, **changes):
        result = plots.register_farmer_plot(farmer, {**self.plot_data, **changes})
        self.assertTrue(result["ok"], result)
        return result["plot_id"]

    def detail(self, plot_id, farmer=91):
        result = plots.get_farmer_plot(farmer, plot_id)
        self.assertTrue(result["ok"], result)
        return result["plot"]


class FarmerPlotIntegrationTests(PlotTestBase):
    # Adapts the existing branch tests for parameterized ownership and precision.
    def test_register_multiple_plots_and_read_owned_details(self):
        first = self.register()
        second = self.register(plot_name="South plot")
        self.assertNotEqual(first, second)
        result = plots.get_farmer_plots(91)
        self.assertEqual(len(result["plots"]), 4)
        self.assertNotIn(21, [p["plot_id"] for p in result["plots"]])
        detail = self.detail(first)
        self.assertEqual(detail["plot_name"], "North plot")
        self.assertEqual(Decimal(detail["area_ha"]), Decimal("0.40468564224"))
        self.assertEqual(Decimal(detail["area_acres"]), 1)
        self.assertNotIn("farmer_id", detail)

    def test_acres_and_hectares_have_one_canonical_value(self):
        acres = self.register()
        data = {key: value for key, value in self.plot_data.items() if key != "area_acres"}
        data["area_ha"] = "0.40468564224000"
        result = plots.register_farmer_plot(91, data)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.detail(acres)["area_ha"], self.detail(result["plot_id"])["area_ha"])
        columns = [r["name"] for r in self.db.execute("PRAGMA table_info(farm_plots)")]
        self.assertIn("area_ha", columns)
        self.assertNotIn("area_acres", columns)
        values, errors = plots._validate_plot_fields({**self.plot_data, "area_acres": "0.001"})
        self.assertFalse(errors)
        self.assertEqual(values["area_ha"], Decimal("0.00040468564224"))

    # Retains the earlier location-pair and standardized-choice validation.
    def test_all_existing_subcounties_and_dependent_wards_are_accepted(self):
        self.assertEqual(PLOT_OPTIONS["county"], "Uasin Gishu")
        self.assertEqual(set(PLOT_OPTIONS["sub_counties"]), {"Ainabkoi", "Kapseret", "Kesses", "Moiben", "Soy", "Turbo"})
        for sub_county, wards in PLOT_OPTIONS["sub_counties"].items():
            for ward in wards:
                with self.subTest(sub_county=sub_county, ward=ward):
                    values, errors = plots._validate_plot_fields({**self.plot_data, "sub_county": sub_county, "ward": ward})
                    self.assertFalse(errors)
                    self.assertEqual(values["ward"], ward)

    def test_invalid_locations_and_partial_administrative_updates_are_rejected(self):
        for changes in ({"county": "Trans Nzoia"}, {"county": "Kitale"}, {"county": "Vihiga"},
                        {"sub_county": "Soy", "ward": "Langas"}, {"ward": "Invented ward"},
                        {"soil_type": "unlisted"}, {"farming_type": "unlisted"}):
            with self.subTest(changes=changes):
                self.assertFalse(plots.register_farmer_plot(91, {**self.plot_data, **changes})["ok"])
        self.assertFalse(plots.update_farmer_plot(91, 11, {"ward": "Ziwa"})["ok"])
        self.assertTrue(plots.update_farmer_plot(91, 11, {"county": "Uasin Gishu", "sub_county": "Soy", "ward": "Ziwa"})["ok"])

    def test_invalid_areas_are_rejected_without_rounding_or_clamping(self):
        for value in (None, True, [], {}, 0, -1, float("nan"), float("inf"), "1e2", "0.0001", "1.1234"):
            with self.subTest(value=value):
                self.assertFalse(plots.register_farmer_plot(91, {**self.plot_data, "area_acres": value})["ok"])
        both = {**self.plot_data, "area_ha": "1"}
        self.assertFalse(plots.register_farmer_plot(91, both)["ok"])
        missing = {k: v for k, v in self.plot_data.items() if k != "area_acres"}
        self.assertFalse(plots.register_farmer_plot(91, missing)["ok"])
        for area in ("0.00001", "10000.00000000000001", "0.123456789012345"):
            self.assertFalse(plots.register_farmer_plot(91, {**missing, "area_ha": area})["ok"])

    def test_ownership_for_reads_updates_and_deletion(self):
        self.assertEqual(plots.get_farmer_plot(91, 21)["code"], "not_found")
        self.assertEqual(plots.update_farmer_plot(91, 21, {"plot_name": "Unauthorized"})["code"], "not_found")
        self.assertEqual(plots.delete_farmer_plot(91, 21)["code"], "not_found")
        self.assertEqual(plots.get_farmer_plot(91, 999)["code"], "not_found")
        self.assertEqual(self.detail(21, farmer=92)["plot_name"], "Existing plot")
        for function, args in ((plots.get_farmer_plots, (True,)),
                               (plots.register_farmer_plot, (0, self.plot_data)),
                               (plots.update_farmer_plot, (-1, 11, {"plot_name": "X"})),
                               (plots.delete_farmer_plot, (None, 11))):
            with self.assertRaises(ValueError): function(*args)

    def test_metadata_update_and_parameterized_text_preserve_legacy_data(self):
        name = "North'); DROP TABLE farm_plots; --"
        self.assertTrue(plots.update_farmer_plot(91, 11, {"plot_name": name, "location": " Landmark "})["ok"])
        self.assertEqual(self.detail(11)["plot_name"], name)
        self.assertEqual(self.detail(11)["location"], "Landmark")
        self.assertEqual(self.detail(11)["area_ha"], "1.0")
        self.assertIsNone(self.detail(11)["county"])
        self.assertFalse(plots.update_farmer_plot(91, 11, {"farmer_id": 92})["ok"])
        self.assertFalse(plots.update_farmer_plot(91, 11, {})["ok"])

    def test_delete_only_unreferenced_plot(self):
        new = self.register()
        self.assertTrue(plots.delete_farmer_plot(91, new)["ok"])
        self.assertEqual(plots.get_farmer_plot(91, new)["code"], "not_found")
        plan = self.create()
        result = plots.delete_farmer_plot(91, 11)
        self.assertEqual(result["code"], "plot_has_plans")
        self.assertEqual(plans.get_plan(91, plan["plan_id"])["plot_id"], 11)
        self.assertEqual(self.count("farmers"), 2)

    def test_new_plot_supports_multiple_seasonal_plans_and_correct_comparison_area(self):
        plot_id = self.register()
        first = self.create(plot=plot_id)
        self.create(plot=plot_id, planting_season="short_rains")
        self.assertEqual(len(plans.list_plans(91, plot_id)), 2)
        result = plans.compare_saved_plan(91, first["plan_id"])
        self.assertEqual(result["area_ha"], 0.40468564224)
        current = result["scenarios"][2]
        self.assertEqual(current["status"], "available")
        self.assertAlmostEqual(current["expected_production_kg"], current["predicted_yield_kg_ha"] * 0.40468564224)
        self.assertAlmostEqual(current["nutrients_kg_ha"]["N"], 27.5 / 0.40468564224)

    def test_area_change_clears_all_affected_selections_only(self):
        first = self.create()
        second = self.create(planting_season="short_rains")
        other = self.create(plot=12)
        for plan in (first, second, other): plans.select_scenario(91, plan["plan_id"], "current")
        result = plots.update_farmer_plot(91, 11, {"area_ha": "2.000"})
        self.assertTrue(result["ok"], result)
        for plan in (first, second):
            updated = plans.get_plan(91, plan["plan_id"])
            self.assertIsNone(updated["selection"])
            self.assertEqual(updated["fertilizers"], plan["fertilizers"])
        self.assertIsNotNone(plans.get_plan(91, other["plan_id"])["selection"])
        current = plans.compare_saved_plan(91, first["plan_id"])["scenarios"][2]
        self.assertAlmostEqual(current["nutrients_kg_ha"]["N"], 27.5 / 2)
        self.assertAlmostEqual(current["expected_production_kg"], current["predicted_yield_kg_ha"] * 2)

    def test_equivalent_area_and_metadata_edits_keep_selection(self):
        plot_id = self.register()
        plan = self.create(plot=plot_id)
        selection = plans.select_scenario(91, plan["plan_id"], "current")
        for changes in ({"area_ha": "0.40468564224000"}, {"area_acres": "1.000"}, {"location": "New landmark"}):
            self.assertTrue(plots.update_farmer_plot(91, plot_id, changes)["ok"])
            self.assertEqual(plans.get_plan(91, plan["plan_id"])["selection"], selection)

    def test_area_and_selection_changes_rollback_together(self):
        plan = self.create()
        selection = plans.select_scenario(91, plan["plan_id"], "current")
        self.connection.fail_commit = True
        self.assertEqual(plots.update_farmer_plot(91, 11, {"area_ha": "2"})["code"], "database_error")
        self.connection.fail_commit = False
        self.assertEqual(self.detail(11)["area_ha"], "1.0")
        self.assertEqual(plans.get_plan(91, plan["plan_id"])["selection"], selection)

    def test_unchanged_validation_input_objects(self):
        original = copy.deepcopy(self.plot_data)
        self.register()
        self.assertEqual(self.plot_data, original)


class FarmerPlotRouteTests(PlotTestBase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-plot-integration")
        self.client = self.app.test_client()
        auth = patch("app.security.get_active_farmer_identity", return_value={
            "ok": True, "identity": {"user_id": 41, "farmer_id": 91}})
        self.auth = auth.start()
        self.addCleanup(auth.stop)

    def login(self):
        with self.client.session_transaction() as session:
            session.update(user_id=41, farmer_id=91, authenticated_at=time.time())
        return {"X-CSRF-Token": self.client.get("/farmer/plots/csrf-token").json["csrf_token"]}

    def test_all_plot_endpoints_require_existing_authentication(self):
        for method, url in (("GET", "/farmer/plots"), ("GET", "/farmer/plots/options"),
                            ("GET", "/farmer/plots/csrf-token"), ("POST", "/farmer/plots"),
                            ("GET", "/farmer/plots/11"), ("PATCH", "/farmer/plots/11"), ("DELETE", "/farmer/plots/11")):
            with self.subTest(method=method, url=url):
                self.assertEqual(self.client.open(url, method=method, json={}).status_code, 401)

    def test_create_list_detail_update_and_delete_flow(self):
        headers = self.login()
        response = self.client.post("/farmer/plots", json=self.plot_data, headers=headers)
        self.assertEqual(response.status_code, 201, response.json)
        plot_id = response.json["plot_id"]
        url = f"/farmer/plots/{plot_id}"
        self.assertEqual(self.client.get(url).json["plot"]["sub_county"], "Soy")
        self.assertEqual(self.client.get(url).headers["Cache-Control"], "no-store")
        self.assertEqual(len(self.client.get("/farmer/plots").json["plots"]), 3)
        self.assertEqual(self.client.patch(url, json={"area_ha": "2"}, headers=headers).status_code, 200)
        self.assertEqual(Decimal(self.client.get(url).json["plot"]["area_ha"]), 2)
        self.assertEqual(self.client.delete(url, headers=headers).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_scoped_csrf_protects_all_mutations(self):
        self.login()
        for method, url, payload in (("POST", "/farmer/plots", self.plot_data),
                                     ("PATCH", "/farmer/plots/11", {"area_ha": "2"}),
                                     ("DELETE", "/farmer/plots/11", {})):
            for token in (None, "incorrect"):
                with self.subTest(method=method, token=token):
                    headers = {} if token is None else {"X-CSRF-Token": token}
                    self.assertEqual(self.client.open(url, method=method, json=payload, headers=headers).status_code, 403)

    def test_owner_payload_does_not_grant_access_and_foreign_ids_are_hidden(self):
        headers = self.login()
        result = self.client.post("/farmer/plots", json={**self.plot_data, "farmer_id": 92}, headers=headers)
        plot_id = result.json["plot_id"]
        self.assertTrue(plots.get_farmer_plot(91, plot_id)["ok"])
        self.assertEqual(plots.get_farmer_plot(92, plot_id)["code"], "not_found")
        for method in ("GET", "PATCH", "DELETE"):
            self.assertEqual(self.client.open("/farmer/plots/21?farmer_id=92", method=method,
                             headers=headers, json={"plot_name": "X"}).status_code, 404)

    def test_options_are_unchanged_and_deleting_plan_plot_is_a_conflict(self):
        headers = self.login()
        self.assertEqual(self.client.get("/farmer/plots/options").json["options"], PLOT_OPTIONS)
        self.create()
        self.assertEqual(self.client.delete("/farmer/plots/11", headers=headers).status_code, 409)

    def test_existing_form_contract_and_decimal_json_are_supported(self):
        headers = self.login()
        response = self.client.post("/farmer/plots", data={**self.plot_data, "csrf_token": headers["X-CSRF-Token"]})
        self.assertEqual(response.status_code, 201, response.json)
        payload = '{"plot_name":"Exact","area_ha":0.40468564224,"sub_county":"Soy","ward":"Ziwa"}'
        response = self.client.post("/farmer/plots", data=payload, content_type="application/json", headers=headers)
        self.assertEqual(response.status_code, 201, response.json)
        detail = self.client.get(f"/farmer/plots/{response.json['plot_id']}").json["plot"]
        self.assertEqual(Decimal(detail["area_ha"]), Decimal("0.40468564224"))

    def test_revoked_identity_and_database_outages_fail_closed(self):
        self.login()
        self.auth.return_value = {"ok": False, "code": "invalid_session"}
        self.assertEqual(self.client.get("/farmer/plots").status_code, 401)
        self.auth.return_value = {"ok": True, "identity": {"user_id": 41, "farmer_id": 91}}
        self.login()
        with patch.object(plots, "get_db_connection", side_effect=Error("Sensitive details")):
            response = self.client.get("/farmer/plots")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Sensitive", response.get_data(as_text=True))
