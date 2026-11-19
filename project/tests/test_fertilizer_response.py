import math
import unittest
from unittest.mock import patch

from app.services.fertilizer_response_service import (
    compare_response, load_response_resources, predict_fertilizer_response,
)


class FertilizerResponseTests(unittest.TestCase):
    def test_reference_equation_and_pairwise_interactions(self):
        model, _ = load_response_resources()
        c = model["coefficients"]
        n, p, k = 1 - math.exp(-1), 1 - math.exp(-1), 1 - math.exp(-20 / 30)
        expected = c[1]*n + c[2]*p + c[3]*k + c[5]*n*p + c[6]*n*k + c[7]*p*k
        self.assertAlmostEqual(expected, 2460.5931451026217)
        for variety in ("623A", "HAC"):
            result = predict_fertilizer_response({"N": 60, "P": 15, "K": 20, "Variety": variety})
            self.assertAlmostEqual(result["response_yield_kg_ha"], expected)
            self.assertEqual(result["output_unit"], "kg/ha_increment_vs_zero_NPK")
            self.assertNotIn("predicted_yield_kg_ha", result)
        for first, second, index in (("N", "P", 5), ("N", "K", 6), ("P", "K", 7)):
            rates = {"N": 0, "P": 0, "K": 0, "Variety": "623A"}
            rates.update({first: model["saturation_scales"][first], second: model["saturation_scales"][second]})
            pair = predict_fertilizer_response(rates)["response_yield_kg_ha"]
            first_only = predict_fertilizer_response({**rates, second: 0})["response_yield_kg_ha"]
            second_only = predict_fertilizer_response({**rates, first: 0})["response_yield_kg_ha"]
            self.assertAlmostEqual(pair - first_only - second_only, c[index] * (1-math.exp(-1))**2)

    def test_zero_reference_for_both_varieties(self):
        for variety in ("623A", "HAC"):
            self.assertEqual(predict_fertilizer_response(
                {"N": 0, "P": 0, "K": 0, "Variety": variety})["response_yield_kg_ha"], 0)

    def test_comparison_returns_response_difference_only(self):
        result = compare_response({"N": 0, "P": 0, "K": 0}, {"N": 60, "P": 15, "K": 20}, "HAC")
        self.assertAlmostEqual(result["response_change_kg_ha"], 2460.5931451026217)
        self.assertNotIn("predicted_yield_kg_ha", result)

    def test_negative_responses_are_not_clipped(self):
        model, metadata = load_response_resources()
        coefficients = [0, -10, 0, 0, 0, 0, 0, 0]
        with patch("app.services.fertilizer_response_service.load_response_resources",
                   return_value=({**model, "coefficients": coefficients}, metadata)):
            result = predict_fertilizer_response({"N": 60, "P": 0, "K": 0, "Variety": "HAC"})
        self.assertLess(result["response_yield_kg_ha"], 0)

    def test_invalid_inputs(self):
        sample = {"N": 60, "P": 15, "K": 20, "Variety": "623A"}
        for name in ("N", "P", "K"):
            for value in (-1, float("nan"), float("inf"), True, [], "bad", None):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    predict_fertilizer_response({**sample, name: value})
        for variety in ("hac", " 623A", False, [], None):
            with self.assertRaises(ValueError):
                predict_fertilizer_response({**sample, "Variety": variety})
        for name in sample:
            missing = sample.copy(); del missing[name]
            with self.assertRaises(ValueError):
                predict_fertilizer_response(missing)
        with patch("app.services.fertilizer_response_service.load_response_resources") as load:
            with self.assertRaises(ValueError):
                predict_fertilizer_response(None)
            load.assert_not_called()
