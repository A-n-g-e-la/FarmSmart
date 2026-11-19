"""Kitale15 fertilizer response, reported separately from total yield."""

import json
import math
from functools import lru_cache

import joblib

from app.services.prediction_service import MODEL_DIR, validate_model_inputs


@lru_cache(maxsize=1)
def load_response_resources():
    """Load the coefficient dictionary and its matching metadata."""
    with (MODEL_DIR / "farmadvis_kitale15_response_metadata.json").open(encoding="utf-8") as file:
        metadata = json.load(file)
    model = joblib.load(MODEL_DIR / "farmadvis_kitale15_response_model.joblib")
    expected_order = ["intercept", "sat_N", "sat_P", "sat_K", "variety_623A",
                      "N_x_P", "N_x_K", "P_x_K"]
    if not isinstance(model, dict):
        raise ValueError("Response artifact must be a coefficient dictionary")
    for key in ("component", "model_type", "feature_order", "saturation_scales", "output_unit"):
        if model[key] != metadata[key]:
            raise ValueError(f"Response artifact disagrees with metadata: {key}")
    if (model["feature_order"] != expected_order
            or model["saturation_scales"] != {"N": 60.0, "P": 15.0, "K": 30.0}
            or model["reference_treatment"] != {"N": 0.0, "P": 0.0, "K": 0.0}
            or model["variety_indicator"] != {"623A": 1, "HAC": 0}
            or metadata["input_features"] != ["N", "P", "K", "Variety"]
            or metadata["supported_varieties"] != ["623A", "HAC"]
            or model["output_unit"] != "kg/ha_increment_vs_zero_NPK"
            or len(model["coefficients"]) != len(expected_order)
            or not all(math.isfinite(value) for value in model["coefficients"])):
        raise ValueError("Unexpected Kitale15 response model contract")
    return model, metadata


def predict_fertilizer_response(input_data):
    """Estimate additional yield relative to zero NPK for the same variety."""
    if not isinstance(input_data, dict):
        raise ValueError("input_data must be a dictionary")
    model, metadata = load_response_resources()
    inputs = validate_model_inputs(input_data, metadata)
    saturation = {name: -math.expm1(-inputs[name] / model["saturation_scales"][name])
                  for name in ("N", "P", "K")}
    n, p, k = (saturation[name] for name in ("N", "P", "K"))
    terms = {"intercept": 1.0, "sat_N": n, "sat_P": p, "sat_K": k,
             "variety_623A": model["variety_indicator"][inputs["Variety"]],
             "N_x_P": n * p, "N_x_K": n * k, "P_x_K": p * k}
    # Subtract the same-variety zero-NPK design row. Intercepts cancel.
    reference = {name: 0.0 for name in model["feature_order"]}
    reference.update(intercept=1.0, variety_623A=terms["variety_623A"])
    response = math.fsum(coefficient * (terms[name] - reference[name])
                         for name, coefficient in zip(model["feature_order"], model["coefficients"]))
    if not math.isfinite(response):
        raise ValueError("Predicted fertilizer response must be finite")
    return {"response_yield_kg_ha": response, "output_unit": metadata["output_unit"],
            "model_name": metadata["model_type"], "site_year": metadata["site_year"],
            "validated_treatment_effects": False,
            "limitations": metadata["limitations"]}


def compare_response(baseline_rates, scenario_rates, variety):
    """Compare response estimates only; never construct an XGBoost total."""
    baseline = predict_fertilizer_response({**baseline_rates, "Variety": variety})
    scenario = predict_fertilizer_response({**scenario_rates, "Variety": variety})
    return {**scenario, "response_change_kg_ha":
            scenario["response_yield_kg_ha"] - baseline["response_yield_kg_ha"]}
