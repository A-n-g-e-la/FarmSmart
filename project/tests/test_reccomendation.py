import copy
import unittest
from unittest.mock import patch

from app.services.recommendation_service import compare_npk_scenarios, compare_plot_plans


class PredictionCallerCompatibilityTests(unittest.TestCase):
    def test_scenario_supplies_all_four_inputs(self):
        with patch("app.services.recommendation_service.predict_maize_yield",
                   return_value={"predicted_yield_kg_ha": 2345.6}) as predict:
            result = compare_npk_scenarios({"Variety": "623A"}, [{"N": 60, "P": 15, "K": 20}])
        self.assertEqual(predict.call_args.args[0], {"N": 60, "P": 15, "K": 20, "Variety": "623A"})
        self.assertEqual(result["scenarios"][0]["predicted_yield_kg_ha"], 2345.6)
        self.assertNotIn("K is not a feature", result["disclaimer"])

    def test_missing_variety_is_reported(self):
        with self.assertRaisesRegex(ValueError, "Missing base inputs:.*Variety"):
            compare_npk_scenarios({}, [{"N": 60, "P": 15, "K": 20}])

    def test_real_scenario_returns_xgboost_total_without_response_addition(self):
        result = compare_npk_scenarios({"Variety": "623A"}, [{"N": 60, "P": 15, "K": 20}])
        self.assertAlmostEqual(result["scenarios"][0]["predicted_yield_kg_ha"], 5008.22, places=2)



class ProductComparisonTests(unittest.TestCase):
    def setUp(self):
        self.data = {
            "plot": {"area_ha": 1},
            "management": {"variety": "623A", "fertilizers": [
                {"product": "UREA", "quantity_kg": 60 / 0.46},
                {"product": "TSP", "quantity_kg": 15 * 2.291 / 0.46},
                {"product": "MOP", "quantity_kg": 20 * 1.205 / 0.60}]},
            "prices": {name: {"price_kes_per_kg": price} for name, price in
                       (("UREA", 60), ("TSP", 80), ("MOP", 70))},
            "farmgate_price_kes_kg": 35,
            "costs": {"seed_kes": 2500, "labour_kes": 4000, "other_kes": 1500},
        }

    def test_requested_scales_current_plan_and_unchanged_product_types(self):
        original = copy.deepcopy(self.data)
        result = compare_plot_plans(self.data)
        self.assertEqual(self.data, original)
        self.assertEqual([s["scaling_percentage"] for s in result["scenarios"]], [0, 75, 100, 125, 150])
        self.assertEqual([s["is_current_plan"] for s in result["scenarios"]], [False, False, True, False, False])
        self.assertEqual(len({s["scenario_id"] for s in result["scenarios"]}), 5)
        for row in result["scenarios"]:
            self.assertEqual(row["status"], "available")
            self.assertEqual([p["product"] for p in row["fertilizer"]["products"]], ["UREA", "TSP", "MOP"])
            for original_product, product in zip(self.data["management"]["fertilizers"], row["fertilizer"]["products"]):
                self.assertAlmostEqual(product["quantity_kg"], original_product["quantity_kg"] * row["scaling_percentage"] / 100)
        current = result["scenarios"][2]
        self.assertAlmostEqual(current["predicted_yield_kg_ha"], 5008.22, places=2)
        self.assertAlmostEqual(current["nutrients_kg_ha"]["N"], 60)
        self.assertAlmostEqual(current["nutrients_kg_ha"]["P"], 15)
        self.assertAlmostEqual(current["nutrients_kg_ha"]["K"], 20)
        self.assertEqual(result["model_limits_kg_ha"], {"N": [0, 120], "P": [0, 22.5], "K": [0, 30]})

    def test_real_yield_and_financial_integrations(self):
        from app.services.prediction_service import predict_maize_yield
        from app.services.roi_service import calculate_plot_roi
        from app.services.fertilizer_service import calculate_product_plan
        for variety in ("623A", "HAC"):
            self.data["management"]["variety"] = variety
            result = compare_plot_plans(self.data)
            for row in result["scenarios"]:
                products = [{"product": p["product"], "quantity_kg": p["quantity_kg"]}
                            for p in row["fertilizer"]["products"]]
                fertilizer = calculate_product_plan(products, 1, self.data["prices"])
                self.assertEqual(row["fertilizer"], fertilizer)
                total = predict_maize_yield({**row["nutrients_kg_ha"], "Variety": variety})["predicted_yield_kg_ha"]
                financial = calculate_plot_roi(total, 35, fertilizer["cost_kes"], 2500, 4000, 1500, area_ha=1)
                self.assertEqual(row["predicted_yield_kg_ha"], total)
                for name in ("expected_production_kg", "fertilizer_cost_kes", "total_cost_kes",
                             "revenue_kes", "net_profit_kes", "roi_percent"):
                    self.assertEqual(row[name], financial[name])
                self.assertAlmostEqual(row["total_cost_kes"] - row["fertilizer_cost_kes"], 8000)

    # Adapted from the preserved no-double-counting comparison test.
    def test_plot_totals_and_finances_do_not_add_response(self):
        with patch("app.services.recommendation_service.predict_maize_yield",
                   return_value={"predicted_yield_kg_ha": 5000}) as predict, patch(
                   "app.services.fertilizer_response_service.compare_response",
                   side_effect=AssertionError("Response must not construct total yield")) as response:
            result = compare_plot_plans(self.data)
        self.assertEqual(predict.call_count, 5)
        response.assert_not_called()
        for row in result["scenarios"]:
            self.assertEqual(row["predicted_yield_kg_ha"], 5000)
            self.assertEqual(row["expected_production_kg"], 5000)
            self.assertEqual(row["revenue_kes"], 175000)

    def test_zero_percent_has_zero_quantities_nutrients_and_fertilizer_cost(self):
        row = compare_plot_plans(self.data)["scenarios"][0]
        self.assertTrue(all(p["quantity_kg"] == 0 for p in row["fertilizer"]["products"]))
        self.assertEqual(row["nutrients_kg_ha"], {"N": 0, "P": 0, "K": 0})
        self.assertEqual(row["fertilizer_cost_kes"], 0)
        self.assertEqual(row["total_cost_kes"], 8000)

    def test_acres_equivalent_to_hectares_and_whole_plot_units(self):
        from app.services.feature_preparation_service import ACRE_TO_HA
        by_ha = compare_plot_plans(self.data)
        self.data["plot"] = {"area_acres": 1 / ACRE_TO_HA}
        by_acres = compare_plot_plans(self.data)
        self.assertEqual(by_ha["scenarios"], by_acres["scenarios"])
        self.data["plot"] = {"area_ha": 2}
        double_area = compare_plot_plans(self.data)
        for row in double_area["scenarios"]:
            self.assertAlmostEqual(row["expected_production_kg"], row["predicted_yield_kg_ha"] * 2)
            self.assertAlmostEqual(row["revenue_kes"], row["expected_production_kg"] * 35)
        self.assertAlmostEqual(double_area["scenarios"][2]["nutrients_kg_ha"]["N"], 30)

    def test_unsupported_scenarios_return_reasons_and_no_financial_estimates(self):
        self.data["management"]["fertilizers"] = [{"product": "UREA", "quantity_kg": 90 / 0.46}]
        with patch("app.services.recommendation_service.predict_maize_yield",
                   return_value={"predicted_yield_kg_ha": 4000}) as predict:
            result = compare_plot_plans(self.data)
        self.assertEqual(len(result["scenarios"]), 5)
        self.assertEqual(predict.call_count, 4)
        unavailable = result["scenarios"][-1]
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertAlmostEqual(unavailable["nutrients_kg_ha"]["N"], 135)
        self.assertIn("N=135", unavailable["unavailable_reason"])
        for name in ("predicted_yield_kg_ha", "expected_production_kg", "total_cost_kes",
                     "revenue_kes", "net_profit_kes", "roi_percent"):
            self.assertIsNone(unavailable[name])

    def test_each_nutrient_limit_is_checked_without_clamping(self):
        for product, grade, rate in (("UREA", 0.46, 121), ("TSP", 0.46 / 2.291, 23), ("MOP", 0.6 / 1.205, 31)):
            with self.subTest(product=product):
                self.data["management"]["fertilizers"] = [{"product": product, "quantity_kg": rate / grade}]
                current = compare_plot_plans(self.data)["scenarios"][2]
                self.assertEqual(current["status"], "unavailable")
                self.assertIsNone(current["net_profit_kes"])
                self.assertAlmostEqual(max(current["nutrients_kg_ha"].values()), rate)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            compare_npk_scenarios({"Variety": "623A"}, [{"N": 130, "P": 0, "K": 0}])

    def test_zero_fertilizer_collapses_duplicates_without_inventing_products(self):
        for products in ([], [{"product": "UREA", "quantity_kg": 0}]):
            self.data["management"]["fertilizers"] = products
            result = compare_plot_plans(self.data)
            self.assertEqual(len(result["scenarios"]), 1)
            self.assertTrue(result["scenarios"][0]["is_current_plan"])
            self.assertEqual(result["scenarios"][0]["scaling_percentage"], 100)
            self.assertEqual(result["scenarios"][0]["fertilizer_cost_kes"], 0)
            self.assertTrue(any("collapsed" in warning for warning in result["warnings"]))

    def test_supplied_total_cost_scales_with_quantities(self):
        self.data["management"]["fertilizers"] = [{"product": "UREA", "quantity_kg": 50,
                                                   "total_cost_kes": 4000}]
        self.data["prices"] = {}
        result = compare_plot_plans(self.data)
        self.assertEqual([r["fertilizer_cost_kes"] for r in result["scenarios"]], [0, 3000, 4000, 5000, 6000])

    def test_custom_grade_is_preserved(self):
        self.data["management"]["fertilizers"] = [{"product": "Blend", "quantity_kg": 50,
            "grade": {"N": 10, "P2O5": 10, "K2O": 10}, "total_cost_kes": 2000}]
        current = compare_plot_plans(self.data)["scenarios"][2]
        self.assertEqual(current["fertilizer"]["products"][0]["grade"], {"N": 10, "P2O5": 10, "K2O": 10})
        self.assertEqual(current["nutrients_kg_ha"]["N"], 5)

    def test_no_automatic_selection_or_ranking(self):
        result = compare_plot_plans(self.data)
        for key in ("preferred_option", "recommended_scenario", "highest_estimated_profit_option", "selected_scenario"):
            self.assertNotIn(key, result)
        self.assertEqual([r["scaling_percentage"] for r in result["scenarios"]], [0, 75, 100, 125, 150])
        self.assertTrue(any("not selected" in w or "No plan is selected" in w for w in result["warnings"]))

    def test_invalid_areas_and_varieties(self):
        for plot in ({}, {"area_ha": 0}, {"area_acres": -1}, {"area_ha": True},
                     {"area_ha": 1, "area_acres": 1}, {"area_ha": float("nan")}):
            with self.subTest(plot=plot), self.assertRaises(ValueError):
                compare_plot_plans({**self.data, "plot": plot})
        for variety in (None, "hac", " 623A", True, "unknown"):
            data = copy.deepcopy(self.data);data["management"]["variety"] = variety
            with self.subTest(variety=variety), self.assertRaises(ValueError):
                compare_plot_plans(data)

    def test_invalid_quantities_grades_prices_and_costs(self):
        for value in (-1, True, None, "bad", float("nan"), float("inf")):
            for kind in ("quantity", "grade", "price", "seed_kes", "labour_kes", "other_kes"):
                data = copy.deepcopy(self.data)
                if kind == "quantity": data["management"]["fertilizers"][0]["quantity_kg"] = value
                elif kind == "grade": data["management"]["fertilizers"][0]["grade"] = {"N": value, "P2O5": 0, "K2O": 0}
                elif kind == "price": data["prices"]["UREA"]["price_kes_per_kg"] = value
                else: data["costs"][kind] = value
                with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                    compare_plot_plans(data)
        for field in ("costs", "management", "plot", "farmgate_price_kes_kg"):
            data = copy.deepcopy(self.data);del data[field]
            with self.assertRaises(ValueError): compare_plot_plans(data)
        with self.assertRaises(ValueError): compare_plot_plans(None)
        with self.assertRaises(ValueError): compare_plot_plans({**self.data, "scenarios": []})

    def test_zero_cost_roi_and_loss_making_options_are_returned(self):
        self.data["costs"] = {"seed_kes": 0, "labour_kes": 0, "other_kes": 0}
        self.assertIsNone(compare_plot_plans(self.data)["scenarios"][0]["roi_percent"])
        self.data["farmgate_price_kes_kg"] = 0
        self.assertTrue(all(r["net_profit_kes"] < 0 for r in compare_plot_plans(self.data)["scenarios"][1:]))

    def test_scaled_product_service_limits_keep_unavailable_options_in_results(self):
        self.data["plot"] = {"area_ha": 10000}
        self.data["management"]["fertilizers"] = [{"product": "UREA", "quantity_kg": 100000}]
        result = compare_plot_plans(self.data)
        self.assertEqual(len(result["scenarios"]), 5)
        self.assertEqual([r["status"] for r in result["scenarios"]],
                         ["available", "available", "available", "unavailable", "unavailable"])
        self.assertIn("Scaled product plan", result["scenarios"][-1]["unavailable_reason"])
        self.assertIsNone(result["scenarios"][-1]["net_profit_kes"])
