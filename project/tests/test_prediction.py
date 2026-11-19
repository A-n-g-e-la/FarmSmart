import math
import unittest
from unittest.mock import patch

import pandas as pd
from sklearn.pipeline import Pipeline

from app.services.prediction_service import load_prediction_resources, predict_maize_yield


class PredictionServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model, cls.metadata = load_prediction_resources()

    def setUp(self):
        self.sample = {"N": 60, "P": 15, "K": 20, "Variety": "623A"}

    def test_exported_components_are_fitted_and_cached(self):
        model, metadata = load_prediction_resources()
        self.assertIsInstance(model, Pipeline)
        self.assertIs(model, self.model)
        self.assertIs(metadata, self.metadata)
        self.assertEqual(load_prediction_resources.cache_info().maxsize, 1)
        self.assertEqual(list(model.feature_names_in_), ["N", "P", "K", "Variety"])
        frame = pd.DataFrame([self.sample], columns=metadata["input_features"])
        self.assertEqual(model.named_steps["preprocessor"].transform(frame).shape, (1, 5))
        self.assertEqual(model.named_steps["regressor"].get_booster().num_features(), 5)

    def test_both_reference_predictions_are_total_kg_ha(self):
        for variety, expected in (("623A", 5008.22), ("HAC", 3766.24)):
            with self.subTest(variety=variety):
                result = predict_maize_yield({**self.sample, "Variety": variety})
                self.assertAlmostEqual(result["predicted_yield_kg_ha"], expected, places=2)
                self.assertTrue(math.isfinite(result["predicted_yield_kg_ha"]))
                self.assertGreaterEqual(result["predicted_yield_kg_ha"], 0)
                self.assertEqual(result["output_unit"], "kg/ha")
                self.assertEqual(result["site_year"], "Kitale15")
                self.assertIn(self.metadata["limitations"], result["disclaimer"])

    def test_feature_order_and_numeric_strings(self):
        sample = dict(reversed(list(self.sample.items())))
        sample.update(N="60", unused="ignored")
        with patch.object(self.model, "predict", return_value=[2345.6]) as predict:
            predict_maize_yield(sample)
        frame = predict.call_args.args[0]
        self.assertEqual(list(frame.columns), ["N", "P", "K", "Variety"])
        self.assertEqual(frame.iloc[0].to_dict(), self.sample)

    def test_missing_features_and_none_are_rejected(self):
        for name in self.sample:
            for missing in (True, False):
                sample = self.sample.copy()
                if missing:
                    del sample[name]
                else:
                    sample[name] = None
                with self.subTest(name=name, missing=missing), self.assertRaisesRegex(ValueError, name):
                    predict_maize_yield(sample)

    def test_invalid_numeric_inputs(self):
        for name in ("N", "P", "K"):
            for value in (True, False, [], {}, object(), "bad", "", float("nan"),
                          float("inf"), -float("inf"), "NaN", "inf", 10**1000, -0.01, "-25"):
                with self.subTest(name=name, value=str(value)), self.assertRaises(ValueError):
                    predict_maize_yield({**self.sample, name: value})

    def test_exact_varieties_only(self):
        for value in ("hac", " 623A", "HAC ", "unknown", "", True, 623, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Variety"):
                predict_maize_yield({**self.sample, "Variety": value})

    def test_zero_rates_are_valid(self):
        result = predict_maize_yield({**self.sample, "N": 0, "P": 0, "K": 0})
        self.assertGreaterEqual(result["predicted_yield_kg_ha"], 0)

    def test_non_dictionary_input_before_loading(self):
        for value in (None, [], "invalid"):
            with patch("app.services.prediction_service.load_prediction_resources") as load:
                with self.assertRaisesRegex(ValueError, "dictionary"):
                    predict_maize_yield(value)
                load.assert_not_called()

    def test_invalid_predictions_are_rejected(self):
        for value in (float("nan"), float("inf"), -float("inf"), -1):
            with patch.object(self.model, "predict", return_value=[value]):
                with self.assertRaisesRegex(ValueError, "finite and nonnegative"):
                    predict_maize_yield(self.sample)
