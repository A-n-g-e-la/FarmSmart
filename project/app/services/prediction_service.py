
"""Kitale15 total maize yield prediction service for FarmAdvis."""

import json
import math
from functools import lru_cache
from numbers import Real
from pathlib import Path

import joblib
import pandas as pd
from sklearn.pipeline import Pipeline
from xgboost import XGBRegressor


# prediction_service.py is in project/app/services/
PROJECT_DIR = Path(__file__).resolve().parents[2]
MODEL_DIR = PROJECT_DIR / "machineLearning" / "models"


@lru_cache(maxsize=1)
def load_prediction_resources():
    """Load the trained model and metadata once per Python process."""

    with (MODEL_DIR / "farmadvis_kitale15_yield_metadata.json").open(encoding="utf-8") as file:
        metadata = json.load(file)
    if (metadata["input_features"] != ["N", "P", "K", "Variety"]
            or metadata["supported_varieties"] != ["623A", "HAC"]
            or metadata["output_unit"] != "kg/ha"):
        raise ValueError("Unexpected Kitale15 yield metadata contract")

    preprocessor = joblib.load(MODEL_DIR / "farmadvis_kitale15_yield_preprocessor.joblib")
    regressor = XGBRegressor()
    regressor.load_model(MODEL_DIR / "farmadvis_kitale15_yield_xgboost.json")
    if list(preprocessor.feature_names_in_) != metadata["input_features"]:
        raise ValueError("Yield preprocessor features disagree with metadata")
    # Reuse fitted components without fitting or loading a combined pickle.
    model = Pipeline([("preprocessor", preprocessor), ("regressor", regressor)])
    return model, metadata


def validate_model_inputs(input_data, metadata):
    """Validate the four inputs shared by the two Kitale15 components."""
    if not isinstance(input_data, dict):
        raise ValueError("input_data must be a dictionary")
    missing = [name for name in metadata["input_features"]
               if name not in input_data or input_data[name] is None]
    if missing:
        raise ValueError(f"Missing required features: {', '.join(missing)}")
    validated = {}
    for name in ("N", "P", "K"):
        raw_value = input_data[name]
        if isinstance(raw_value, bool) or not isinstance(raw_value, (Real, str)):
            raise ValueError(f"{name} must be a numeric value")
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} must be a numeric value") from exc
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite numeric value")
        if value < 0:
            raise ValueError(f"{name} fertilizer rate cannot be negative")
        validated[name] = value
    variety = input_data["Variety"]
    if not isinstance(variety, str) or variety not in metadata["supported_varieties"]:
        raise ValueError("Variety must be exactly 623A or HAC")
    validated["Variety"] = variety
    return validated


def predict_maize_yield(input_data):
    """Predict total yield in kg/ha from N, P, K and Variety."""
    if not isinstance(input_data, dict):
        raise ValueError("input_data must be a dictionary")
    model, metadata = load_prediction_resources()
    validated = validate_model_inputs(input_data, metadata)
    # Preserve the exact feature names and order used during training.
    features = pd.DataFrame([validated], columns=metadata["input_features"])
    # The native model already predicts total yield in kg/ha.
    predicted_yield = float(model.predict(features)[0])
    if not math.isfinite(predicted_yield) or predicted_yield < 0:
        raise ValueError("Predicted yield must be finite and nonnegative")
    return {
        "predicted_yield_kg_ha": predicted_yield,
        "model_name": metadata["model_type"],
        "site_year": metadata["site_year"],
        "output_unit": metadata["output_unit"],
        "disclaimer": f"Experimental yield estimate, not a guaranteed harvest. {metadata['limitations']}",
    }
