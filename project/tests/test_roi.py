"""Financial service tests with supplied yields and whole-plot costs."""

import unittest

from app.services.feature_preparation_service import ACRE_TO_HA
from app.services.fertilizer_service import calculate_product_plan
from app.services.roi_service import calculate_plot_roi, calculate_roi


class RoiServiceTests(unittest.TestCase):
    def setUp(self):
        self.inputs = {
            "predicted_yield_kg_ha": 4000,
            "farmgate_price_kes_kg": 30,
            "fertilizer_cost_kes": 10000,
            "seed_cost_kes": 2000,
            "labour_cost_kes": 3000,
            "other_cost_kes": 1000,
            "area_ha": 2,
        }

    # Financial identities adapted from the preserved decision-support tests.
    def test_whole_plot_cost_revenue_profit_and_roi(self):
        result = calculate_plot_roi(**self.inputs)
        self.assertEqual(result["expected_production_kg"], 8000)
        self.assertEqual(result["fertilizer_cost_kes"], 10000)
        self.assertEqual(result["total_cost_kes"], 16000)
        self.assertEqual(result["revenue_kes"], 240000)
        self.assertEqual(result["net_profit_kes"], 224000)
        self.assertEqual(result["roi_percent"], 1400)
        self.assertEqual(result["revenue_kes"], result["expected_production_kg"] * 30)
        self.assertEqual(result["total_cost_kes"], sum(self.inputs[name] for name in (
            "fertilizer_cost_kes", "seed_cost_kes", "labour_cost_kes", "other_cost_kes")))
        self.assertEqual(result["net_profit_kes"], result["revenue_kes"] - result["total_cost_kes"])
        self.assertEqual(result["roi_percent"], 100 * result["net_profit_kes"] / result["total_cost_kes"])

    def test_per_hectare_and_whole_plot_results_are_consistent(self):
        for area in (0.25, 1, 1.7, 3):
            with self.subTest(area=area):
                result = calculate_plot_roi(**{**self.inputs, "area_ha": area})
                expected = calculate_roi(4000, 30, 10000 / area, 2000 / area, 3000 / area, 1000 / area)
                self.assertEqual(result["per_ha"], expected)
                for key in ("fertilizer_cost_kes", "seed_cost_kes", "labour_cost_kes",
                            "other_cost_kes", "total_cost_kes", "revenue_kes", "net_profit_kes"):
                    self.assertAlmostEqual(result[key], expected[key + "_ha"] * area)
                self.assertEqual(result["roi_percent"], expected["roi_percent"])

    def test_acres_and_hectares_are_equivalent(self):
        inputs = {key: value for key, value in self.inputs.items() if key != "area_ha"}
        for acres in (0.5, 1, 2):
            with self.subTest(acres=acres):
                by_acres = calculate_plot_roi(**inputs, area_acres=acres)
                by_hectares = calculate_plot_roi(**inputs, area_ha=acres * ACRE_TO_HA)
                self.assertEqual(by_acres, by_hectares)
                self.assertAlmostEqual(by_acres["expected_production_kg"], 4000 * acres * 0.40468564224)

    def test_fertilizer_service_cost_is_consumed_directly(self):
        fertilizer = calculate_product_plan(
            [{"product": "DAP", "quantity_kg": 25}, {"product": "UREA", "quantity_kg": 25}],
            prices={"DAP": {"price_kes_per_bag": 4000, "bag_weight_kg": 50},
                    "UREA": {"price_kes_per_bag": 3500, "bag_weight_kg": 50}},
            area_acres=1,
        )
        result = calculate_plot_roi(
            predicted_yield_kg_ha=5000, farmgate_price_kes_kg=35,
            fertilizer_cost_kes=fertilizer["cost_kes"], seed_cost_kes=2500,
            labour_cost_kes=4000, other_cost_kes=1500, area_ha=fertilizer["area_ha"],
        )
        self.assertEqual(result["fertilizer_cost_kes"], 3750)
        self.assertAlmostEqual(result["total_cost_kes"], 11750)
        self.assertAlmostEqual(result["expected_production_kg"], 5000 * ACRE_TO_HA)
        self.assertAlmostEqual(result["revenue_kes"], 5000 * ACRE_TO_HA * 35)
        self.assertAlmostEqual(result["net_profit_kes"], 5000 * ACRE_TO_HA * 35 - 11750)
        self.assertAlmostEqual(result["per_ha"]["fertilizer_cost_kes_ha"], fertilizer["cost_kes_ha"])

    def test_supplied_total_yield_is_used_without_an_additional_response(self):
        result = calculate_plot_roi(**{**self.inputs, "predicted_yield_kg_ha": 5008.22})
        self.assertEqual(result["predicted_yield_kg_ha"], 5008.22)
        self.assertEqual(result["expected_production_kg"], 10016.44)

    # Preserve the existing zero-cost contract: undefined ROI is None.
    def test_zero_cost_roi_is_null(self):
        inputs = {**self.inputs, "fertilizer_cost_kes": 0, "seed_cost_kes": 0,
                  "labour_cost_kes": 0, "other_cost_kes": 0}
        for yield_value in (0, 4000):
            with self.subTest(yield_value=yield_value):
                result = calculate_plot_roi(**{**inputs, "predicted_yield_kg_ha": yield_value})
                self.assertIsNone(result["roi_percent"])
                self.assertIsNone(result["per_ha"]["roi_percent"])
                self.assertEqual(result["total_cost_kes"], 0)
                self.assertEqual(result["net_profit_kes"], result["revenue_kes"])
        self.assertIsNone(calculate_roi(4000, 30, 0, 0, 0, 0)["roi_percent"])

    def test_zero_yield_with_costs(self):
        result = calculate_plot_roi(**{**self.inputs, "predicted_yield_kg_ha": 0})
        self.assertEqual(result["expected_production_kg"], 0)
        self.assertEqual(result["revenue_kes"], 0)
        self.assertEqual(result["net_profit_kes"], -16000)
        self.assertEqual(result["roi_percent"], -100)

    def test_loss_making_scenario(self):
        result = calculate_plot_roi(**{**self.inputs, "predicted_yield_kg_ha": 100})
        self.assertEqual(result["revenue_kes"], 6000)
        self.assertEqual(result["net_profit_kes"], -10000)
        self.assertEqual(result["roi_percent"], -62.5)

    def test_zero_selling_price(self):
        result = calculate_plot_roi(**{**self.inputs, "farmgate_price_kes_kg": 0})
        self.assertEqual(result["expected_production_kg"], 8000)
        self.assertEqual(result["net_profit_kes"], -16000)
        self.assertEqual(result["roi_percent"], -100)

    def test_numeric_strings_are_accepted(self):
        self.assertEqual(calculate_plot_roi(**self.inputs),
                         calculate_plot_roi(**{key: str(value) for key, value in self.inputs.items()}))

    def test_invalid_financial_inputs(self):
        for field in self.inputs:
            if field == "area_ha":
                continue
            for value in (-1, True, False, None, "bad", "", [], {}, 1 + 2j,
                          float("nan"), float("inf"), -float("inf"), "NaN", 10**1000):
                with self.subTest(field=field, value=str(value)), self.assertRaises(ValueError):
                    calculate_plot_roi(**{**self.inputs, field: value})

    def test_missing_values_are_rejected(self):
        for field in self.inputs:
            inputs = self.inputs.copy()
            del inputs[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                calculate_plot_roi(**inputs)

    def test_invalid_and_ambiguous_areas(self):
        inputs = {key: value for key, value in self.inputs.items() if key != "area_ha"}
        for field in ("area_ha", "area_acres"):
            for value in (0, -1, True, None, "", "bad", [], float("nan"), float("inf"), 10**1000):
                with self.subTest(field=field, value=str(value)), self.assertRaises(ValueError):
                    calculate_plot_roi(**inputs, **{field: value})
        with self.assertRaisesRegex(ValueError, "exactly one"):
            calculate_plot_roi(**self.inputs, area_acres=1)

    # Reuses the preserved direct-calculator invalid/overflow cases.
    def test_existing_roi_rejects_invalid_values_and_overflow(self):
        for value in (True, -1, float("inf"), "bad"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate_roi(value, 35, 100, 100, 100, 100)
        with self.assertRaisesRegex(ValueError, "Calculated financial returns must be finite"):
            calculate_roi(1e308, 1e308, 100, 100, 100, 100)

    def test_nonfinite_calculated_plot_results_are_rejected(self):
        for values in (
            {"predicted_yield_kg_ha": 1e308, "farmgate_price_kes_kg": 0, "area_ha": 10000},
            {"predicted_yield_kg_ha": 1e308, "farmgate_price_kes_kg": 1e308},
            {"area_ha": 10000, "fertilizer_cost_kes": 1e308, "seed_cost_kes": 1e308},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                calculate_plot_roi(**{**self.inputs, **values})

    def test_positive_cost_does_not_underflow_to_zero_cost_roi(self):
        with self.assertRaises(ValueError):
            calculate_plot_roi(**{**self.inputs, "area_ha": 10000, "fertilizer_cost_kes": 5e-324,
                                  "seed_cost_kes": 0, "labour_cost_kes": 0, "other_cost_kes": 0})

    def test_input_dictionary_is_unchanged(self):
        before = self.inputs.copy()
        calculate_plot_roi(**self.inputs)
        self.assertEqual(self.inputs, before)
