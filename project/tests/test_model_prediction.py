"""Offline checks for final artifact contracts; no inference at collection."""

import inspect
import unittest
from unittest.mock import patch

from app.services import prediction_service, fertilizer_response_service


class ModelArtifactTests(unittest.TestCase):
    def test_active_services_reference_only_final_artifact_names(self):
        sources = inspect.getsource(prediction_service) + inspect.getsource(fertilizer_response_service)
        for obsolete in ("farmadvis_vihiga", "yield_model.joblib", "decision_support",
                         "all_features", "numeric_features", "pilot_county"):
            self.assertNotIn(obsolete, sources)
        for name in ("farmadvis_kitale15_yield_preprocessor.joblib",
                     "farmadvis_kitale15_yield_xgboost.json",
                     "farmadvis_kitale15_yield_metadata.json",
                     "farmadvis_kitale15_response_model.joblib",
                     "farmadvis_kitale15_response_metadata.json"):
            self.assertIn(name, sources)
            self.assertTrue((prediction_service.MODEL_DIR / name).is_file())

    def test_response_artifact_is_a_cached_dictionary(self):
        model, metadata = fertilizer_response_service.load_response_resources()
        self.assertIsInstance(model, dict)
        self.assertFalse(hasattr(model, "predict"))
        self.assertIs(model, fertilizer_response_service.load_response_resources()[0])
        self.assertEqual(model["feature_order"], metadata["feature_order"])
        self.assertEqual(len(model["coefficients"]), 8)

    def test_loaders_only_deserialize_the_two_approved_joblib_components(self):
        original_load = prediction_service.joblib.load
        with patch.object(prediction_service.joblib, "load", wraps=original_load) as load:
            prediction_service.load_prediction_resources.__wrapped__()
            fertilizer_response_service.load_response_resources.__wrapped__()
        self.assertEqual([call.args[0].name for call in load.call_args_list], [
            "farmadvis_kitale15_yield_preprocessor.joblib",
            "farmadvis_kitale15_response_model.joblib",
        ])

    def test_response_metadata_disagreement_is_rejected(self):
        model, _ = fertilizer_response_service.load_response_resources()
        with patch.object(fertilizer_response_service.joblib, "load", return_value={
                **model, "saturation_scales": {"N": 100, "P": 15, "K": 30}}):
            with self.assertRaisesRegex(ValueError, "disagrees with metadata"):
                fertilizer_response_service.load_response_resources.__wrapped__()
