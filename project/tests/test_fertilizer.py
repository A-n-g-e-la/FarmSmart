"""Focused fertilizer calculations; no API, database or ML inference required."""

import copy
import unittest

from app.services.feature_preparation_service import ACRE_TO_HA
from app.services.fertilizer_service import (
    calculate_fertilizer_costs,
    calculate_fertilizer_requirements,
    calculate_product_plan,
)


class FertilizerServiceTests(unittest.TestCase):
    def setUp(self):
        self.prices = {
            "DAP": {"price_kes_per_bag": 4000, "bag_weight_kg": 50},
            "UREA": {"price_kes_per_bag": 3500, "bag_weight_kg": 50},
            "MOP": {"price_kes_per_bag": 3500, "bag_weight_kg": 50},
        }
        self.products = [{"product": "DAP", "quantity_kg": 50},
                         {"product": "UREA", "quantity_kg": 50}]

    # Adapted from the preserved test_decision_support.py.
    def test_label_conversion_and_product_cost(self):
        plan = calculate_product_plan(self.products, 1, self.prices)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["N"], 32)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["P"], 23 / 2.291)
        self.assertEqual(plan["nutrients_kg_ha"]["K"], 0)
        self.assertEqual(plan["cost_kes"], 7500)
        self.assertEqual(plan["cost_kes_ha"], 7500)
        self.assertEqual(plan["pricing_method"], "proportional_bag_cost")

    # Retains the preserved product-validation cases.
    def test_product_validation(self):
        for value in (-1, True, "bad", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate_product_plan([{"product": "DAP", "quantity_kg": value}], 1, self.prices)
        with self.assertRaisesRegex(ValueError, "prices.DAP"):
            calculate_product_plan([{"product": "DAP", "quantity_kg": 1}], 1, {})
        with self.assertRaisesRegex(ValueError, "grade"):
            calculate_product_plan([{"product": "unknown", "quantity_kg": 1}], 1, {})

    def test_multiple_products_include_potassium(self):
        products = self.products + [{"product": "MOP", "quantity_kg": 50}]
        plan = calculate_product_plan(products, 2, self.prices)
        self.assertEqual(plan["nutrients_kg_ha"]["N"], 16)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["P"], 23 / 2.291 / 2)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["K"], 30 / 1.205 / 2)
        self.assertEqual(plan["cost_kes"], 11000)
        self.assertEqual(plan["cost_kes_ha"], 5500)

    def test_one_acre_conversion_and_whole_plot_cost(self):
        plan = calculate_product_plan(self.products, prices=self.prices, area_acres=1)
        self.assertEqual(plan["area_ha"], 0.40468564224)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["N"], 32 / ACRE_TO_HA)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["P"], 23 / 2.291 / ACRE_TO_HA)
        self.assertEqual(plan["cost_kes"], 7500)
        self.assertAlmostEqual(plan["cost_kes_ha"], 7500 / ACRE_TO_HA)

    def test_acres_and_hectares_give_identical_results(self):
        for acres in (0.5, 1, 2.5):
            with self.subTest(acres=acres):
                self.assertEqual(
                    calculate_product_plan(self.products, acres * ACRE_TO_HA, self.prices),
                    calculate_product_plan(self.products, prices=self.prices, area_acres=acres),
                )

    def test_custom_grade_and_supplied_total_cost(self):
        plan = calculate_product_plan([{
            "product": "Farm blend", "quantity_kg": 50,
            "grade": {"N": 10, "P2O5": 20, "K2O": 30}, "total_cost_kes": 1000,
        }], area_ha=2)
        self.assertEqual(plan["nutrients_kg_ha"]["N"], 2.5)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["P"], 5 / 2.291)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["K"], 7.5 / 1.205)
        self.assertEqual(plan["cost_kes"], 1000)
        self.assertEqual(plan["cost_kes_ha"], 500)
        self.assertEqual(plan["pricing_method"], "supplied_total_cost")

    def test_explicit_grade_overrides_catalog_grade(self):
        plan = calculate_product_plan([{
            "product": "UREA", "quantity_kg": 50,
            "grade": {"N": 20, "P2O5": 0, "K2O": 0}, "total_cost_kes": 500,
        }], 1)
        self.assertEqual(plan["nutrients_kg_ha"]["N"], 10)

    def test_partial_bag_cost_without_rounding(self):
        plan = calculate_product_plan([{"product": "DAP", "quantity_kg": 25}], 1, self.prices)
        self.assertEqual(plan["cost_kes"], 2000)
        self.assertEqual(plan["products"][0]["bags_equivalent"], 0.5)

    def test_per_kg_price_and_numeric_strings(self):
        plan = calculate_product_plan([{"product": " urea ", "quantity_kg": "25"}], "1",
                                      {"UREA": {"price_kes_per_kg": "80"}})
        self.assertEqual(plan["cost_kes"], 2000)
        self.assertEqual(plan["nutrients_kg_ha"]["N"], 11.5)
        self.assertEqual(plan["products"][0]["product"], "UREA")
        self.assertIsNone(plan["products"][0]["bags_equivalent"])

    def test_supplied_total_overrides_catalog_quote(self):
        plan = calculate_product_plan([{"product": "UREA", "quantity_kg": 25,
                                        "total_cost_kes": 1250}], 1, self.prices)
        self.assertEqual(plan["cost_kes"], 1250)

    def test_mixed_pricing_methods(self):
        products = [{"product": "UREA", "quantity_kg": 10},
                    {"product": "DAP", "quantity_kg": 20},
                    {"product": "MOP", "quantity_kg": 15, "total_cost_kes": 100}]
        prices = {**self.prices, "UREA": {"price_kes_per_kg": 2}}
        plan = calculate_product_plan(products, 1, prices)
        self.assertEqual(plan["cost_kes"], 1720)
        self.assertAlmostEqual(plan["nutrients_kg_ha"]["N"], 8.2)
        self.assertEqual(plan["pricing_method"], "mixed_product_costs")

    def test_zero_fertilizer_and_zero_quantity(self):
        for products in ([], [{"product": "UREA", "quantity_kg": 0}]):
            with self.subTest(products=products):
                plan = calculate_product_plan(products, 1)
                self.assertEqual(plan["cost_kes"], 0)
                self.assertEqual(plan["cost_kes_ha"], 0)
                self.assertEqual(plan["nutrients_kg_ha"], {"N": 0, "P": 0, "K": 0})

    def test_zero_price_is_valid(self):
        plan = calculate_product_plan([{"product": "UREA", "quantity_kg": 25}], 1,
                                      {"UREA": {"price_kes_per_kg": 0}})
        self.assertEqual(plan["cost_kes"], 0)
        self.assertEqual(plan["nutrients_kg_ha"]["N"], 11.5)

    def test_invalid_quantities(self):
        for value in (None, False, [], {}, "", -0.01, "-5", -float("inf"), 10**1000, 100001):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                calculate_product_plan([{"product": "DAP", "quantity_kg": value}], 1, self.prices)

    def test_invalid_prices_and_total_costs(self):
        for field in ("price_kes_per_bag", "price_kes_per_kg", "total_cost_kes"):
            for value in (-1, True, None, "bad", "", [], {}, float("nan"), float("inf")):
                item = {"product": "UREA", "quantity_kg": 25}
                quote = {field: value, "bag_weight_kg": 50}
                if field == "total_cost_kes":
                    item[field] = value
                    quote = {}
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    calculate_product_plan([item], 1, {"UREA": quote})
        with self.assertRaisesRegex(ValueError, "not both"):
            calculate_product_plan([{"product": "UREA", "quantity_kg": 1}], 1,
                                   {"UREA": {"price_kes_per_kg": 50, "price_kes_per_bag": 2500}})

    def test_invalid_bag_weights(self):
        for value in (0, -1, True, None, "bad", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate_product_plan([{"product": "UREA", "quantity_kg": 25}], 1,
                                       {"UREA": {"price_kes_per_bag": 3500, "bag_weight_kg": value}})

    def test_invalid_or_ambiguous_area(self):
        for field in ("area_ha", "area_acres"):
            for value in (0, -1, True, None, "bad", [], float("nan"), float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    calculate_product_plan([], **{field: value})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            calculate_product_plan([], 1, area_acres=1)

    def test_invalid_product_grades(self):
        for grade in (None, {}, {"N": 1, "P2O5": 2}, {"N": 101, "P2O5": 0, "K2O": 0},
                      {"N": 40, "P2O5": 40, "K2O": 40}, {"N": True, "P2O5": 0, "K2O": 0},
                      {"N": -1, "P2O5": 0, "K2O": 0}, {"N": float("nan"), "P2O5": 0, "K2O": 0}):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                calculate_product_plan([{"product": "Custom", "quantity_kg": 1,
                                         "grade": grade, "total_cost_kes": 1}], 1)

    def test_invalid_input_containers_and_names(self):
        for products in (None, {}, "UREA", [None], [{"product": "", "quantity_kg": 1}],
                         [{"product": 123, "quantity_kg": 1}], self.products * 7):
            with self.subTest(products=products), self.assertRaises(ValueError):
                calculate_product_plan(products, 1, self.prices)
        with self.assertRaises(ValueError):
            calculate_product_plan([], 1, prices=[])

    def test_input_objects_are_not_modified(self):
        before = copy.deepcopy((self.products, self.prices))
        calculate_product_plan(self.products, 1, self.prices)
        self.assertEqual((self.products, self.prices), before)

    def test_existing_requirement_and_cost_functions_remain_available(self):
        grades = {"urea": {"nitrogen_pct": 46}, "tsp": {"phosphorus_p2o5_pct": 46},
                  "mop": {"potassium_k2o_pct": 60}}
        requirements = calculate_fertilizer_requirements(46, 20, 30, grades)["requirements"]
        self.assertEqual(requirements["urea_kg_ha"], 100)
        self.assertAlmostEqual(requirements["tsp_kg_ha"], 20 * 2.291 / 0.46)
        self.assertAlmostEqual(requirements["mop_kg_ha"], 30 * 1.205 / 0.6)
        quotes = {name: {"price_kes_per_bag": 2500, "bag_weight_kg": 50} for name in grades}
        costs = calculate_fertilizer_costs(requirements, quotes)
        self.assertAlmostEqual(costs["fertilizer_cost_kes_ha"], sum(requirements.values()) * 50)
