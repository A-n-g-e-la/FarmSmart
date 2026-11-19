"""Isolated persistence and authorization tests; never connect to live MySQL."""

import copy
import sqlite3
import time
import unittest
from unittest.mock import patch

import dotenv
from mysql.connector import Error, IntegrityError

with patch.object(dotenv, "load_dotenv", return_value=False):
    from app import create_app
    from app.models import agricultural_plan as model
    from app.services import agricultural_plan_service as service


# Equivalent constraints for isolated tests; production DDL is a manual MySQL script.
TEST_SCHEMA = """
CREATE TABLE farmers (farmer_id INTEGER PRIMARY KEY);
CREATE TABLE farm_plots (plot_id INTEGER PRIMARY KEY, farmer_id INTEGER NOT NULL
 REFERENCES farmers(farmer_id), area_ha REAL NOT NULL);
CREATE TABLE agricultural_plans (
 plan_id INTEGER PRIMARY KEY AUTOINCREMENT, plot_id INTEGER NOT NULL REFERENCES farm_plots(plot_id),
 planting_year INTEGER NOT NULL, planting_season TEXT NOT NULL, variety TEXT NOT NULL,
 farmgate_price_kes_kg REAL NOT NULL, seed_cost_kes REAL NOT NULL, labour_cost_kes REAL NOT NULL,
 other_cost_kes REAL NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 updated_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(plot_id, planting_year, planting_season));
CREATE TABLE agricultural_plan_products (
 plan_product_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL
 REFERENCES agricultural_plans(plan_id) ON DELETE CASCADE, position INTEGER NOT NULL,
 product_name TEXT NOT NULL, nitrogen_pct REAL NOT NULL, phosphorus_p2o5_pct REAL NOT NULL,
 potassium_k2o_pct REAL NOT NULL, quantity_kg REAL NOT NULL, pricing_json TEXT NOT NULL,
 cost_kes REAL NOT NULL, UNIQUE(plan_id, position));
CREATE TABLE agricultural_plan_selections (
 plan_id INTEGER PRIMARY KEY REFERENCES agricultural_plans(plan_id) ON DELETE CASCADE,
 scenario_id TEXT NOT NULL, scaling_percentage INTEGER NOT NULL, products_json TEXT NOT NULL,
 selected_at TEXT DEFAULT CURRENT_TIMESTAMP);
INSERT INTO farmers VALUES (91), (92);
INSERT INTO farm_plots VALUES (11,91,1), (12,91,2), (21,92,0.5);
"""


class TestCursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def execute(self, sql, params=()):
        self.cursor.execute(sql.replace("%s", "?").replace(" FOR UPDATE", ""), params)

    @property
    def lastrowid(self):
        return self.cursor.lastrowid

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    def close(self):
        self.cursor.close()


class TestConnection:
    def __init__(self, connection):
        self.connection = connection
        self.fail_commit = False
        self.rollbacks = 0

    def start_transaction(self):
        self.connection.execute("BEGIN")

    def cursor(self, dictionary=False):
        return TestCursor(self.connection)

    def commit(self):
        if self.fail_commit:
            raise Error("Simulated commit failure")
        self.connection.commit()

    def rollback(self):
        self.rollbacks += 1
        self.connection.rollback()

    def close(self):
        # Keep only the isolated test database alive between service calls.
        pass


class PlanTestBase(unittest.TestCase):
    def setUp(self):
        guard = patch("mysql.connector.connect", side_effect=AssertionError("Live MySQL forbidden"))
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.guard.assert_not_called)
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(TEST_SCHEMA)
        self.addCleanup(self.db.close)
        self.connection = TestConnection(self.db)
        connect = patch.object(service, "get_db_connection", return_value=self.connection)
        connect.start()
        self.addCleanup(connect.stop)
        self.data = {
            "planting_year": 2026, "planting_season": "long_rains", "variety": "623A",
            "farmgate_price_kes_kg": 35, "seed_cost_kes": 1000,
            "labour_cost_kes": 2000, "other_cost_kes": 500,
            "fertilizers": [{"product": "UREA", "quantity_kg": 50},
                            {"product": "DAP", "quantity_kg": 25}],
            "prices": {"UREA": {"price_kes_per_kg": 70},
                       "DAP": {"price_kes_per_bag": 4000, "bag_weight_kg": 50}},
        }

    def create(self, plot=11, farmer=91, **changes):
        return service.create_plan(farmer, plot, {**copy.deepcopy(self.data), **changes})

    def count(self, table):
        # Table names are fixed by these tests, never supplied by a user.
        return self.db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]


class AgriculturalPlanTests(PlanTestBase):
    def test_create_retrieve_multiple_products_and_price_snapshot(self):
        original = copy.deepcopy(self.data)
        plan = self.create()
        self.assertEqual(self.data, original)
        self.assertEqual(plan["plot_id"], 11)
        self.assertNotIn("farmer_id", plan)
        self.assertEqual(plan["area_ha"], 1)
        self.assertEqual(len(plan["fertilizers"]), 2)
        self.assertEqual(plan["fertilizers"][0]["grade"], {"N": 46, "P2O5": 0, "K2O": 0})
        self.assertEqual(plan["fertilizers"][0]["pricing"], {"price_kes_per_kg": 70})
        self.assertEqual(plan["fertilizers"][1]["pricing"], {"price_kes_per_bag": 4000, "bag_weight_kg": 50})
        self.assertEqual(sum(p["cost_kes"] for p in plan["fertilizers"]), 5500)
        self.assertIsNone(plan["selection"])
        self.assertTrue(plan["created_at"])
        self.assertEqual(service.get_plan(91, plan["plan_id"]), plan)

    def test_multiple_plots_years_and_seasons(self):
        first = self.create()
        self.create(planting_season="short_rains")
        self.create(planting_year=2027)
        other_plot = self.create(plot=12)
        self.assertEqual(len(service.list_plans(91, 11)), 3)
        self.assertEqual(len(service.list_plans(91, 12)), 1)
        self.assertEqual(service.get_season_plan(91, 11, "2026", "Long Rains")["plan_id"], first["plan_id"])
        self.assertEqual(other_plot["area_ha"], 2)

    def test_duplicate_season_is_prevented_by_service_and_constraint(self):
        self.create()
        with self.assertRaises(service.DuplicateSeason):
            self.create(planting_season=" LONG RAINS ")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO agricultural_plans (plot_id,planting_year,planting_season,variety,"
                            "farmgate_price_kes_kg,seed_cost_kes,labour_cost_kes,other_cost_kes) "
                            "VALUES (11,2026,'long_rains','623A',35,0,0,0)")
        self.assertEqual(self.count("agricultural_plans"), 1)

    def test_mysql_duplicate_race_is_a_safe_conflict(self):
        with patch.object(model, "insert_plan", side_effect=IntegrityError(errno=1062)):
            with self.assertRaises(service.DuplicateSeason):
                self.create()
        self.assertEqual(self.count("agricultural_plans"), 0)

    def test_missing_or_foreign_plot_and_plan_are_not_accessible(self):
        other = self.create(plot=21, farmer=92)
        for operation, args in ((service.create_plan, (91, 21, self.data)),
                                (service.list_plans, (91, 21)),
                                (service.get_plan, (91, other["plan_id"])),
                                (service.update_plan, (91, other["plan_id"], {"variety": "HAC"})),
                                (service.delete_plan, (91, other["plan_id"])),
                                (service.compare_saved_plan, (91, other["plan_id"])),
                                (service.select_scenario, (91, other["plan_id"], "current")),
                                (service.get_plan, (91, 999))):
            with self.subTest(operation=operation.__name__), self.assertRaises(service.PlanNotFound):
                operation(*args)

    def test_invalid_seasons_varieties_and_missing_fields(self):
        for field, value in (("planting_year", True), ("planting_year", 2026.5),
                             ("planting_year", 999), ("planting_season", "summer"),
                             ("variety", "hac"), ("variety", [])):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.create(**{field: value})
        for field in ("variety", "planting_year", "planting_season", "seed_cost_kes", "fertilizers"):
            data = copy.deepcopy(self.data)
            del data[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                service.create_plan(91, 11, data)
        self.assertEqual(self.count("agricultural_plans"), 0)

    def test_invalid_prices_costs_and_quantities_do_not_save_any_rows(self):
        for value in (-1, True, "bad", None, float("nan"), float("inf")):
            for field in ("farmgate_price_kes_kg", "seed_cost_kes", "labour_cost_kes", "other_cost_kes",
                          "quantity", "price", "grade"):
                data = copy.deepcopy(self.data)
                if field == "quantity": data["fertilizers"][1]["quantity_kg"] = value
                elif field == "price": data["prices"]["UREA"]["price_kes_per_kg"] = value
                elif field == "grade": data["fertilizers"][1]["grade"] = {"N": value, "P2O5": 0, "K2O": 0}
                else: data[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    service.create_plan(91, 11, data)
        self.assertEqual(self.count("agricultural_plans"), 0)
        self.assertEqual(self.count("agricultural_plan_products"), 0)

    def test_custom_grade_direct_total_and_zero_fertilizer_round_trip(self):
        plan = self.create(fertilizers=[{"product": "Blend", "quantity_kg": 20,
            "grade": {"N": 10, "P2O5": 20, "K2O": 30}, "total_cost_kes": 800}], prices={})
        self.assertEqual(plan["fertilizers"][0]["pricing"], {"total_cost_kes": 800})
        self.assertEqual(service.compare_saved_plan(91, plan["plan_id"])["scenarios"][2]["fertilizer_cost_kes"], 800)
        zero = self.create(plot=12, fertilizers=[], prices={})
        self.assertEqual(len(service.compare_saved_plan(91, zero["plan_id"])["scenarios"]), 1)

    def test_updates_are_complete_and_preserve_immutable_fields(self):
        plan = self.create()
        updated = service.update_plan(91, plan["plan_id"], {"variety": "HAC", "seed_cost_kes": 1500,
                    "prices": {"UREA": {"price_kes_per_kg": 80}, "DAP": self.data["prices"]["DAP"]}})
        self.assertEqual(updated["variety"], "HAC")
        self.assertEqual(updated["seed_cost_kes"], 1500)
        self.assertEqual(updated["fertilizers"][0]["cost_kes"], 4000)
        for key in ("plan_id", "plot_id", "planting_year", "planting_season", "created_at"):
            self.assertEqual(updated[key], plan[key])
        replaced = service.update_plan(91, plan["plan_id"], {"fertilizers": [], "prices": {}})
        self.assertEqual(replaced["fertilizers"], [])
        for changes in ({}, {"plot_id": 12}, {"planting_year": 2027}, {"password": "ignored-secret"}):
            with self.assertRaises(ValueError): service.update_plan(91, plan["plan_id"], changes)

    def test_quantity_updates_use_saved_price_and_clear_the_selection(self):
        plan = self.create()
        service.select_scenario(91, plan["plan_id"], "current")
        updated = service.update_plan(91, plan["plan_id"], {
            "fertilizers": [{"product": "UREA", "quantity_kg": 75}]})
        self.assertEqual(updated["fertilizers"][0]["cost_kes"], 5250)
        self.assertIsNone(updated["selection"])
        self.assertEqual(self.count("agricultural_plan_products"), 1)

    def test_unrepresentable_combined_costs_are_rejected_before_persistence(self):
        with self.assertRaises(ValueError):
            self.create(seed_cost_kes=1e308, labour_cost_kes=1e308)
        self.assertEqual(self.count("agricultural_plans"), 0)

    def test_recommendations_reuse_saved_products_and_stored_area_without_saving_options(self):
        from app.services.recommendation_service import compare_plot_plans
        plan = self.create(plot=12)
        result = service.compare_saved_plan(91, plan["plan_id"])
        self.assertEqual([r["scaling_percentage"] for r in result["scenarios"]], [0,75,100,125,150])
        current = result["scenarios"][2]
        self.assertEqual(current["fertilizer_cost_kes"], 5500)
        self.assertAlmostEqual(current["nutrients_kg_ha"]["N"], 27.5 / 2)
        self.assertEqual(current["expected_production_kg"], current["predicted_yield_kg_ha"] * 2)
        with patch.object(service, "compare_plot_plans", wraps=compare_plot_plans) as compare:
            service.compare_saved_plan(91, plan["plan_id"])
        self.assertEqual(compare.call_count, 1)
        self.assertEqual(compare.call_args.args[0]["plot"], {"area_ha": 2})
        self.assertEqual(self.count("agricultural_plan_selections"), 0)
        self.assertEqual(service.get_plan(91, plan["plan_id"]), plan)

    def test_save_and_change_selection_preserves_original_plan(self):
        plan = self.create()
        choice = service.select_scenario(91, plan["plan_id"], "reduced")
        self.assertEqual(choice["plan_id"], plan["plan_id"])
        self.assertEqual(choice["scaling_percentage"], 75)
        self.assertEqual(choice["fertilizer_products"][0]["quantity_kg"], 37.5)
        choice = service.select_scenario(91, plan["plan_id"], "higher")
        self.assertEqual(choice["fertilizer_products"][0]["quantity_kg"], 75)
        self.assertEqual(self.count("agricultural_plan_selections"), 1)
        saved = service.get_plan(91, plan["plan_id"])
        self.assertEqual(saved["fertilizers"], plan["fertilizers"])
        self.assertEqual(saved["selection"], choice)

    def test_unavailable_unknown_and_other_plan_options_are_rejected(self):
        plan = self.create(fertilizers=[{"product": "UREA", "quantity_kg": 250}])
        for choice in ("higher", "not-a-scenario", None):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                service.select_scenario(91, plan["plan_id"], choice)
        zero = self.create(plot=12, fertilizers=[])
        with self.assertRaises(ValueError):
            service.select_scenario(91, zero["plan_id"], "reduced")
        self.assertEqual(self.count("agricultural_plan_selections"), 0)

    def test_valid_edits_clear_selection_and_invalid_edits_preserve_it(self):
        plan = self.create()
        choice = service.select_scenario(91, plan["plan_id"], "current")
        with self.assertRaises(ValueError):
            service.update_plan(91, plan["plan_id"], {"variety": "HAC", "seed_cost_kes": -1})
        self.assertEqual(service.get_plan(91, plan["plan_id"])["selection"], choice)
        self.assertEqual(service.get_plan(91, plan["plan_id"])["variety"], "623A")
        updated = service.update_plan(91, plan["plan_id"], {"farmgate_price_kes_kg": 40})
        self.assertIsNone(updated["selection"])

    def test_product_insert_failure_rolls_back_parent_and_children(self):
        original = model.replace_products
        def partial_failure(cursor, plan_id, products):
            original(cursor, plan_id, products[:1])
            raise Error("Simulated product failure")
        with patch.object(model, "replace_products", side_effect=partial_failure):
            with self.assertRaises(Error): self.create()
        self.assertEqual(self.count("agricultural_plans"), 0)
        self.assertEqual(self.count("agricultural_plan_products"), 0)
        self.assertGreater(self.connection.rollbacks, 0)

    def test_failed_update_restores_original_products_and_selection(self):
        plan = self.create()
        service.select_scenario(91, plan["plan_id"], "current")
        before = service.get_plan(91, plan["plan_id"])
        original = model.replace_products
        def partial_failure(cursor, plan_id, products):
            original(cursor, plan_id, products[:1])
            raise Error("Simulated update failure")
        with patch.object(model, "replace_products", side_effect=partial_failure):
            with self.assertRaises(Error):
                service.update_plan(91, plan["plan_id"], {"variety": "HAC"})
        self.assertEqual(service.get_plan(91, plan["plan_id"]), before)

    def test_failed_selection_change_and_commit_roll_back(self):
        plan = self.create()
        before = service.select_scenario(91, plan["plan_id"], "reduced")
        self.connection.fail_commit = True
        with self.assertRaises(Error): service.select_scenario(91, plan["plan_id"], "higher")
        self.connection.fail_commit = False
        self.assertEqual(service.get_plan(91, plan["plan_id"])["selection"], before)

    def test_delete_cascades_only_plan_children(self):
        plan = self.create()
        service.select_scenario(91, plan["plan_id"], "current")
        service.delete_plan(91, plan["plan_id"])
        for table in ("agricultural_plans", "agricultural_plan_products", "agricultural_plan_selections"):
            self.assertEqual(self.count(table), 0)
        self.assertEqual(self.count("farm_plots"), 3)
        self.assertEqual(self.count("farmers"), 2)


class AgriculturalPlanRouteTests(PlanTestBase):
    def setUp(self):
        super().setUp()
        self.app = create_app()
        self.app.config.update(TESTING=True, SECRET_KEY="isolated-plan-tests")
        self.client = self.app.test_client()
        auth = patch("app.security.get_active_farmer_identity", return_value={
            "ok": True, "identity": {"user_id": 41, "farmer_id": 91}})
        self.auth = auth.start()
        self.addCleanup(auth.stop)

    def login(self):
        with self.client.session_transaction() as session:
            session.update(user_id=41, farmer_id=91, authenticated_at=time.time())
        token = self.client.get("/farmer/agricultural-plans/csrf").json["csrf_token"]
        return {"X-CSRF-Token": token}

    def test_authentication_is_required_for_every_endpoint(self):
        for method, url in (("GET", "/farmer/agricultural-plans/csrf"),
            ("GET", "/farmer/plots/11/agricultural-plans"), ("POST", "/farmer/plots/11/agricultural-plans"),
            ("GET", "/farmer/agricultural-plans/1"), ("PATCH", "/farmer/agricultural-plans/1"),
            ("DELETE", "/farmer/agricultural-plans/1"), ("GET", "/farmer/agricultural-plans/1/comparisons"),
            ("PUT", "/farmer/agricultural-plans/1/selection")):
            with self.subTest(method=method, url=url):
                self.assertEqual(self.client.open(url, method=method, json={}).status_code, 401)

    def test_csrf_json_and_owner_boundaries(self):
        headers = self.login()
        url = "/farmer/plots/11/agricultural-plans"
        self.assertEqual(self.client.post(url, json=self.data).status_code, 403)
        self.assertEqual(self.client.post(url, headers=headers, data="not json").status_code, 415)
        self.assertEqual(self.client.post(url, headers=headers, json=[]).status_code, 400)
        self.assertEqual(self.client.post("/farmer/plots/21/agricultural-plans", headers=headers, json=self.data).status_code, 404)
        spoofed = {**self.data, "farmer_id": 92}
        self.assertEqual(self.client.post(url, headers=headers, json=spoofed).status_code, 400)
        self.assertEqual(self.count("agricultural_plans"), 0)

    def test_full_farmer_route_flow(self):
        headers = self.login()
        collection = "/farmer/plots/11/agricultural-plans"
        response = self.client.post(collection, headers=headers, json=self.data)
        self.assertEqual(response.status_code, 201, response.json)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        plan_id = response.json["result"]["plan_id"]
        url = f"/farmer/agricultural-plans/{plan_id}"
        self.assertEqual(len(self.client.get(collection).json["result"]), 1)
        self.assertEqual(self.client.get(collection + "?year=2026&season=long_rains").json["result"]["plan_id"], plan_id)
        self.assertEqual(self.client.post(collection, headers=headers, json=self.data).status_code, 409)
        self.assertEqual(len(self.client.get(url + "/comparisons").json["result"]["scenarios"]), 5)
        selected = self.client.put(url + "/selection", headers=headers, json={"scenario_id": "current"})
        self.assertEqual(selected.status_code, 200, selected.json)
        self.assertEqual(self.client.get(url).json["result"]["selection"]["scenario_id"], "current")
        self.assertEqual(self.client.patch(url, headers=headers, json={"variety": "HAC"}).status_code, 200)
        self.assertIsNone(self.client.get(url).json["result"]["selection"])
        self.assertEqual(self.client.delete(url, headers=headers, json={}).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_foreign_plan_is_hidden_for_all_operations(self):
        other = self.create(plot=21, farmer=92)
        headers = self.login()
        url = f"/farmer/agricultural-plans/{other['plan_id']}"
        for method, suffix, data in (("GET", "", {}), ("PATCH", "", {"variety": "HAC"}),
            ("DELETE", "", {}), ("GET", "/comparisons", {}), ("PUT", "/selection", {"scenario_id": "current"})):
            with self.subTest(method=method):
                self.assertEqual(self.client.open(url + suffix, method=method, json=data, headers=headers).status_code, 404)

    def test_invalid_selection_or_forged_snapshot_cannot_be_saved(self):
        plan = self.create()
        headers = self.login()
        url = f"/farmer/agricultural-plans/{plan['plan_id']}/selection"
        self.assertEqual(self.client.put(url, headers=headers, json={"scenario_id": "invented"}).status_code, 400)
        self.assertEqual(self.client.put(url, headers=headers, json={"scenario_id": "current", "products": []}).status_code, 400)
        self.assertEqual(self.count("agricultural_plan_selections"), 0)

    def test_disabled_identity_and_officer_only_session_are_rejected(self):
        self.login()
        self.auth.return_value = {"ok": False, "code": "invalid_session"}
        self.assertEqual(self.client.get("/farmer/plots/11/agricultural-plans").status_code, 401)
        with self.client.session_transaction() as session:
            session.update(user_id=42, agricultural_officer_id=7, authenticated_at=time.time())
        self.assertEqual(self.client.get("/farmer/plots/11/agricultural-plans").status_code, 401)

    def test_database_failure_has_safe_response(self):
        self.login()
        with patch.object(service, "get_db_connection", side_effect=Error("Sensitive SQL diagnostics")):
            response = self.client.get("/farmer/plots/11/agricultural-plans")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Sensitive", response.get_data(as_text=True))
