
"""Experimental financial return calculator for FarmAdvis."""

import math
from numbers import Real


def calculate_roi(
    predicted_yield_kg_ha,
    farmgate_price_kes_kg,
    fertilizer_cost_kes_ha,
    seed_cost_kes_ha,
    labour_cost_kes_ha,
    other_cost_kes_ha,
):
    """
    Calculate projected maize financial returns per hectare.

    Yield: kg/ha
    Selling price: KES/kg
    Production costs: KES/ha
    """

    inputs = {
        "predicted_yield_kg_ha": predicted_yield_kg_ha,
        "farmgate_price_kes_kg": farmgate_price_kes_kg,
        "fertilizer_cost_kes_ha": fertilizer_cost_kes_ha,
        "seed_cost_kes_ha": seed_cost_kes_ha,
        "labour_cost_kes_ha": labour_cost_kes_ha,
        "other_cost_kes_ha": other_cost_kes_ha,
    }

    validated = {}

    # Validate all financial inputs.
    for name, value in inputs.items():
        if isinstance(value, bool) or not isinstance(value, (Real, str)):
            raise ValueError(f"{name} must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(
                f"{name} must be numeric"
            ) from exc

        if not math.isfinite(number):
            raise ValueError(
                f"{name} must be finite"
            )

        if number < 0:
            raise ValueError(
                f"{name} cannot be negative"
            )

        validated[name] = number

    # Calculate total production costs.
    total_cost = (
        validated["fertilizer_cost_kes_ha"]
        + validated["seed_cost_kes_ha"]
        + validated["labour_cost_kes_ha"]
        + validated["other_cost_kes_ha"]
    )

    # Calculate expected revenue.
    revenue = (
        validated["predicted_yield_kg_ha"]
        * validated["farmgate_price_kes_kg"]
    )

    # Calculate projected net profit.
    net_profit = revenue - total_cost

    # ROI is undefined when total costs are zero.
    roi_percent = (
        (net_profit / total_cost) * 100
        if total_cost > 0
        else None
    )

    if not all(math.isfinite(value) for value in (total_cost, revenue, net_profit)) or (
        roi_percent is not None and not math.isfinite(roi_percent)
    ):
        raise ValueError("Calculated financial returns must be finite")

    return {
        "predicted_yield_kg_ha": validated["predicted_yield_kg_ha"],
        "revenue_kes_ha": revenue,
        "fertilizer_cost_kes_ha": validated["fertilizer_cost_kes_ha"],
        "seed_cost_kes_ha": validated["seed_cost_kes_ha"],
        "labour_cost_kes_ha": validated["labour_cost_kes_ha"],
        "other_cost_kes_ha": validated["other_cost_kes_ha"],
        "total_cost_kes_ha": total_cost,
        "net_profit_kes_ha": net_profit,
        "roi_percent": roi_percent,
        "model_status": "experimental",
        "disclaimer": (
            "Financial projection based on experimental yield "
            "estimates and supplied reference prices and costs. "
            "Not a guaranteed financial return."
        ),
    }


def calculate_plot_roi(
    predicted_yield_kg_ha=None,
    farmgate_price_kes_kg=None,
    fertilizer_cost_kes=None,
    seed_cost_kes=None,
    labour_cost_kes=None,
    other_cost_kes=None,
    *,
    area_ha=None,
    area_acres=None,
):
    """Calculate whole-plot financial results from a supplied total-yield estimate.

    Supply exactly one plot area, in hectares or acres. All cost inputs are
    KES for the whole plot; fertilizer_cost_kes accepts the fertilizer
    service's cost_kes directly. Selling price is KES/kg. No yield model or
    fertilizer-response calculation runs here. ROI is None for zero costs.
    """
    from app.services.feature_preparation_service import ACRE_TO_HA, number

    if (area_ha is None) == (area_acres is None):
        raise ValueError("Supply exactly one of area_ha or area_acres")
    if area_acres is not None:
        area_ha = number(area_acres, "area_acres") * ACRE_TO_HA
    area_ha = number(area_ha, "area_ha", 0.0001, 10000)
    predicted_yield = number(predicted_yield_kg_ha, "predicted_yield_kg_ha")
    price = number(farmgate_price_kes_kg, "farmgate_price_kes_kg")
    costs = {
        "fertilizer_cost_kes": fertilizer_cost_kes,
        "seed_cost_kes": seed_cost_kes,
        "labour_cost_kes": labour_cost_kes,
        "other_cost_kes": other_cost_kes,
    }
    costs = {name: number(value, name) for name, value in costs.items()}
    if any(value > 0 and value / area_ha == 0 for value in costs.values()):
        raise ValueError("Production costs are too small to represent per hectare")

    # Convert whole-plot costs once and reuse the existing per-hectare calculator.
    per_ha = calculate_roi(
        predicted_yield_kg_ha=predicted_yield,
        farmgate_price_kes_kg=price,
        fertilizer_cost_kes_ha=costs["fertilizer_cost_kes"] / area_ha,
        seed_cost_kes_ha=costs["seed_cost_kes"] / area_ha,
        labour_cost_kes_ha=costs["labour_cost_kes"] / area_ha,
        other_cost_kes_ha=costs["other_cost_kes"] / area_ha,
    )
    totals = {
        "expected_production_kg": predicted_yield * area_ha,
        "total_cost_kes": per_ha["total_cost_kes_ha"] * area_ha,
        "revenue_kes": per_ha["revenue_kes_ha"] * area_ha,
        "net_profit_kes": per_ha["net_profit_kes_ha"] * area_ha,
    }
    if not all(math.isfinite(value) for value in totals.values()):
        raise ValueError("Calculated financial returns must be finite")

    return {
        "predicted_yield_kg_ha": predicted_yield,
        "area_ha": area_ha,
        "farmgate_price_kes_kg": price,
        **costs,
        **totals,
        "roi_percent": per_ha["roi_percent"],
        "per_ha": per_ha,
        "model_status": per_ha["model_status"],
        "disclaimer": per_ha["disclaimer"],
    }
