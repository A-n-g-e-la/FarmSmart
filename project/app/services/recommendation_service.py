
"""Experimental NPK scenario comparison service for FarmAdvis."""

import math

from app.services.prediction_service import (
    load_prediction_resources,
    predict_maize_yield,
)

from app.services.fertilizer_service import (
    calculate_fertilizer_requirements,
    calculate_fertilizer_costs,
)
from app.services.roi_service import calculate_roi

def compare_npk_scenarios(base_inputs, scenarios):
    """
    Compare predicted maize yields across NPK scenarios.

    The Kitale15 model uses N, P, K and an explicitly supplied Variety.

    base_inputs: dictionary of non-fertilizer model features.
    scenarios: list of dictionaries with name, N, P and K.

    All nutrient rates are in kg/ha.
    """

    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Provide at least one NPK scenario")
    if not isinstance(base_inputs, dict):
        raise ValueError("base_inputs must be a dictionary")

    _, metadata = load_prediction_resources()

    # The reference environment must be supplied explicitly by the caller.
    # This service does not silently substitute historical values.
    non_fertilizer_features = [
        feature for feature in metadata["input_features"]
        if feature not in ("N", "P", "K")
    ]

    missing = [
        feature for feature in non_fertilizer_features
        if feature not in base_inputs or base_inputs[feature] is None
    ]

    if missing:
        raise ValueError(
            f"Missing base inputs: {', '.join(missing)}"
        )

    results = []

    for index, scenario in enumerate(scenarios, start=1):
        if not isinstance(scenario, dict):
            raise ValueError(f"Scenario {index} must be a dictionary")

        missing_rates = [
            rate for rate in ("N", "P", "K")
            if rate not in scenario
        ]

        if missing_rates:
            raise ValueError(
                f"Scenario {index} missing: {', '.join(missing_rates)}"
            )

        scenario_name = scenario.get("name", f"Scenario {index}")

        model_inputs = {
            feature: base_inputs[feature]
            for feature in non_fertilizer_features
        }

        # Map existing scenario names to the trained model's nutrient features.
        model_inputs.update({
            "N": scenario["N"],
            "P": scenario["P"],
            "K": scenario["K"],
        })

        from app.services.prediction_service import validate_model_inputs
        validated = validate_model_inputs(model_inputs, metadata)
        reason = unsupported_nutrient_reason(validated, model_nutrient_limits(metadata))
        if reason:
            raise ValueError(f"Scenario {index} unavailable: {reason}")
        prediction = predict_maize_yield(validated)

        results.append({
            "scenario": scenario_name,
            "N_kg_ha": float(scenario["N"]),
            "P_kg_ha": float(scenario["P"]),
            "K_kg_ha": float(scenario["K"]),
            "predicted_yield_kg_ha": prediction[
                "predicted_yield_kg_ha"
            ],
        })

    return {
        "model_status": "experimental",
        "scenarios": results,
        "disclaimer": (
            "These are experimental model scenario estimates, "
            "for Kitale15 only. They are "
            "not validated fertilizer treatment effects or "
            "agricultural recommendations."
        ),
    }


def compare_npk_financial_scenarios(
    base_inputs,
    scenarios,
    fertilizer_products,
    fertilizer_prices,
    farmgate_price_kes_kg,
    seed_cost_kes_ha,
    labour_cost_kes_ha,
    other_cost_kes_ha,
):
    """
    Compare experimental NPK scenarios using predicted
    yield, fertilizer costs and projected financial returns.

    All production costs are expressed in KES/ha.
    """

    # Get the ML predictions for all NPK scenarios.
    prediction_results = compare_npk_scenarios(
        base_inputs,
        scenarios,
    )

    financial_results = []

    for scenario in prediction_results["scenarios"]:

        # Convert nutrient rates into fertilizer quantities.
        requirements = calculate_fertilizer_requirements(
            nitrogen_kg_ha=scenario["N_kg_ha"],
            phosphorus_kg_ha=scenario["P_kg_ha"],
            potassium_kg_ha=scenario["K_kg_ha"],
            fertilizer_products=fertilizer_products,
        )

        # Calculate fertilizer costs.
        fertilizer_costs = calculate_fertilizer_costs(
            requirements["requirements"],
            fertilizer_prices,
        )

        # Calculate projected financial returns.
        roi = calculate_roi(
            predicted_yield_kg_ha=scenario[
                "predicted_yield_kg_ha"
            ],
            farmgate_price_kes_kg=farmgate_price_kes_kg,
            fertilizer_cost_kes_ha=fertilizer_costs[
                "fertilizer_cost_kes_ha"
            ],
            seed_cost_kes_ha=seed_cost_kes_ha,
            labour_cost_kes_ha=labour_cost_kes_ha,
            other_cost_kes_ha=other_cost_kes_ha,
        )

        financial_results.append({
            **scenario,
            "fertilizer_requirements": requirements[
                "requirements"
            ],
            "fertilizer_costs": fertilizer_costs,
            "financial_projection": roi,
        })

    return {
        "model_status": "experimental",
        "scenarios": financial_results,
        "disclaimer": (
            "Experimental model predictions and financial "
            "projections only. NPK scenario differences are "
            "not validated treatment effects or agricultural "
            "recommendations. Prices and costs must be "
            "verified before practical use."
        ),
    }


def compare_plot_plans(data):
    """Compare fixed scales of a farmer's products without selecting a plan.

    plot: exactly one of area_ha or area_acres.
    management: variety (623A/HAC) and fertilizers in whole-plot kg.
    prices: fertilizer-service quotes; item total_cost_kes scales with quantity.
    costs: seed_kes, labour_kes and other_kes for the whole plot (required).
    farmgate_price_kes_kg: supplied maize selling price.
    """
    from app.services.feature_preparation_service import number, object_input
    from app.services.fertilizer_service import calculate_product_plan
    from app.services.prediction_service import validate_model_inputs
    from app.services.roi_service import calculate_plot_roi

    object_input(data, "request")
    plot = object_input(data.get("plot"), "plot")
    management = object_input(data.get("management"), "management")
    prices = object_input(data.get("prices", {}), "prices")
    costs = object_input(data.get("costs"), "costs")
    cost_values = {key: number(costs.get(key), f"costs.{key}")
                   for key in ("seed_kes", "labour_kes", "other_kes")}
    farmgate = number(data.get("farmgate_price_kes_kg"), "farmgate_price_kes_kg")
    if "scenarios" in data:
        raise ValueError("Scenarios are generated from the current products; do not supply alternatives")
    current_items = management.get("fertilizers")
    baseline = calculate_product_plan(current_items, area_ha=plot.get("area_ha"),
                                      area_acres=plot.get("area_acres"), prices=prices)
    area_ha = baseline["area_ha"]
    _, metadata = load_prediction_resources()
    variety = management.get("variety")
    validate_model_inputs({**baseline["nutrients_kg_ha"], "Variety": variety}, metadata)
    limits = model_nutrient_limits(metadata)
    warnings = [
        "Exploratory comparisons: estimated yields and financial results are not guaranteed field outcomes.",
        metadata["limitations"],
        "Total yield comes directly from XGBoost; the independent fertilizer-response estimate is not added.",
        "Quantities and supplied product total costs scale proportionally; seed, labour and other costs stay fixed.",
        "Observed nutrient ranges do not validate every combination within them; scaled treatments may be unobserved.",
        "The current plan is unchanged. No plan is selected or saved to the database.",
    ]
    scales = [(0, "no_fertilizer", "No fertilizer"),
              (75, "reduced", "Reduced application"),
              (100, "current", "Current farmer plan"),
              (125, "increased", "Increased application"),
              (150, "higher", "Higher application")]
    if not any(item["quantity_kg"] > 0 for item in baseline["products"]):
        scales = [(100, "current", "Current farmer plan (no fertilizer)")]
        warnings.append("The current plan has zero fertilizer. Duplicate zero-quantity options were collapsed; no products or doses were invented.")

    results = []
    for percentage, identifier, name in scales:
        factor = percentage / 100
        items = []
        for original, product in zip(current_items, baseline["products"]):
            item = {**original, "quantity_kg": product["quantity_kg"] * factor}
            if "total_cost_kes" in original:
                item["total_cost_kes"] = product["cost_kes"] * factor
            items.append(item)
        row = {
            "scenario_id": identifier, "name": name, "scaling_percentage": percentage,
            "is_current_plan": percentage == 100,
            "fertilizer": {"products": items}, "nutrients_kg_ha": None,
            "fertilizer_cost_kes": None,
            "status": "unavailable", "unavailable_reason": None,
            "predicted_yield_kg_ha": None, "expected_production_kg": None,
            "total_cost_kes": None, "revenue_kes": None, "net_profit_kes": None, "roi_percent": None,
        }
        try:
            fertilizer = calculate_product_plan(items, area_ha, prices)
        except ValueError as exc:
            # The original plan was validated; scaling can exceed service limits.
            row["unavailable_reason"] = f"Scaled product plan cannot be evaluated: {exc}"
            results.append(row)
            continue
        reason = unsupported_nutrient_reason(fertilizer["nutrients_kg_ha"], limits)
        row.update(fertilizer=fertilizer, nutrients_kg_ha=fertilizer["nutrients_kg_ha"],
                   fertilizer_cost_kes=fertilizer["cost_kes"],
                   status="unavailable" if reason else "available", unavailable_reason=reason)
        if not reason:
            prediction = predict_maize_yield({**fertilizer["nutrients_kg_ha"], "Variety": variety})
            financial = calculate_plot_roi(
                predicted_yield_kg_ha=prediction["predicted_yield_kg_ha"], area_ha=area_ha,
                farmgate_price_kes_kg=farmgate, fertilizer_cost_kes=fertilizer["cost_kes"],
                seed_cost_kes=cost_values["seed_kes"], labour_cost_kes=cost_values["labour_kes"],
                other_cost_kes=cost_values["other_kes"],
            )
            row.update(financial)
        results.append(row)
    return {
        "ok": True, "model_status": "experimental_comparison", "area_ha": area_ha,
        "variety": variety, "scenarios": results, "model_limits_kg_ha": limits,
        "warnings": warnings,
        "units": {"yield": "kg/ha", "production": "kg per whole plot",
                  "product_quantity": "kg per whole plot", "costs_and_returns": "KES per whole plot",
                  "nutrient_rates": "kg/ha", "roi": "percent"},
    }


def model_nutrient_limits(metadata):
    """Use the observed training ranges, not invented or clamped limits."""
    combinations = metadata["observed_npk_combinations"]
    return {name: [min(row[index] for row in combinations), max(row[index] for row in combinations)]
            for index, name in enumerate(("N", "P", "K"))}


def unsupported_nutrient_reason(rates, limits):
    """Return an explanation when a scenario lies outside observed ranges."""
    # Allow only floating-point conversion noise at a boundary; never clamp rates.
    outside = [f"{name}={rates[name]:g} kg/ha outside [{low:g}, {high:g}]"
               for name, (low, high) in limits.items()
               if not (low <= rates[name] <= high
                       or math.isclose(rates[name], low, rel_tol=1e-12, abs_tol=1e-12)
                       or math.isclose(rates[name], high, rel_tol=1e-12, abs_tol=1e-12))]
    return "; ".join(outside) if outside else None
