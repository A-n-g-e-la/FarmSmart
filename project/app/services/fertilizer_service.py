
"""Experimental fertilizer quantity calculations for FarmAdvis."""

import math


# Provisional assumption:
# Model N, P and K application rates are elemental kg/ha.
#
# Commercial fertilizer grades use N-P2O5-K2O percentages.
# This assumption has not been independently verified
# against the original experimental dataset.

P_TO_P2O5 = 2.291
K_TO_K2O = 1.205


def calculate_fertilizer_requirements(
    nitrogen_kg_ha,
    phosphorus_kg_ha,
    potassium_kg_ha,
    fertilizer_products,
):
    """
    Calculate fertilizer quantities per hectare.

    fertilizer_products must contain:
        urea: nitrogen_pct
        tsp: phosphorus_p2o5_pct
        mop: potassium_k2o_pct

    Product percentages are expressed on a 0-100 scale.
    """

    nutrients = {
        "N": nitrogen_kg_ha,
        "P": phosphorus_kg_ha,
        "K": potassium_kg_ha,
    }

    for name, value in nutrients.items():
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{name} must be numeric"
            ) from exc

        if not math.isfinite(number) or number < 0:
            raise ValueError(
                f"{name} must be finite and non-negative"
            )

        nutrients[name] = number

    product_fields = {
        "urea": "nitrogen_pct",
        "tsp": "phosphorus_p2o5_pct",
        "mop": "potassium_k2o_pct",
    }

    percentages = {}

    for product, field in product_fields.items():
        if product not in fertilizer_products:
            raise ValueError(
                f"Missing fertilizer product: {product}"
            )

        try:
            percentage = float(
                fertilizer_products[product][field]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid {field} for {product}"
            ) from exc

        if not math.isfinite(percentage) or not 0 < percentage <= 100:
            raise ValueError(
                f"{field} for {product} must be between 0 and 100"
            )

        percentages[product] = percentage / 100

    # Convert elemental P and K to oxide equivalents.
    phosphorus_p2o5 = nutrients["P"] * P_TO_P2O5
    potassium_k2o = nutrients["K"] * K_TO_K2O

    requirements = {
        "urea_kg_ha": (
            nutrients["N"] / percentages["urea"]
        ),
        "tsp_kg_ha": (
            phosphorus_p2o5 / percentages["tsp"]
        ),
        "mop_kg_ha": (
            potassium_k2o / percentages["mop"]
        ),
    }

    return {
        "requirements": requirements,
        "nutrient_basis": "elemental_assumed",
        "disclaimer": (
            "Experimental calculation assuming elemental N, P and K. "
            "Product grades must be verified. "
            "Not a validated fertilizer recommendation."
        ),
    }

#Cost Calculation

def calculate_fertilizer_costs(requirements, fertilizer_prices):
    """
    Calculate fertilizer costs per hectare.

    requirements:
        Output from calculate_fertilizer_requirements()["requirements"]

    fertilizer_prices:
        Dictionary containing price_kes_per_bag and bag_weight_kg
        for urea, tsp and mop.
    """

    product_keys = {
        "urea": "urea_kg_ha",
        "tsp": "tsp_kg_ha",
        "mop": "mop_kg_ha",
    }

    cost_breakdown = {}
    total_cost = 0.0

    for product, requirement_key in product_keys.items():
        try:
            quantity = float(requirements[requirement_key])
            price = float(
                fertilizer_prices[product]["price_kes_per_bag"]
            )
            bag_weight = float(
                fertilizer_prices[product]["bag_weight_kg"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Missing or invalid quantity/price for {product}"
            ) from exc

        if not all(
            math.isfinite(value)
            for value in (quantity, price, bag_weight)
        ):
            raise ValueError(
                f"Values for {product} must be finite"
            )

        if quantity < 0 or price < 0 or bag_weight <= 0:
            raise ValueError(
                f"Invalid quantity, price or bag weight for {product}"
            )

        bags_required = quantity / bag_weight
        cost = bags_required * price

        cost_breakdown[product] = {
            "quantity_kg_ha": quantity,
            "bags_required_per_ha": bags_required,
            "price_kes_per_bag": price,
            "cost_kes_ha": cost,
        }

        total_cost += cost

    return {
        "fertilizer_cost_kes_ha": total_cost,
        "breakdown": cost_breakdown,
        "pricing_method": "proportional_bag_cost",
        "disclaimer": (
            "Costs use supplied reference prices and proportional "
            "bag quantities. Actual purchasing costs may differ."
        ),
    }


# Label percentages are N-P2O5-K2O; prices must be supplied by the caller.
PRODUCT_GRADES = {
    "UREA": {"N": 46, "P2O5": 0, "K2O": 0},
    "DAP": {"N": 18, "P2O5": 46, "K2O": 0},
    "TSP": {"N": 0, "P2O5": 46, "K2O": 0},
    "MOP": {"N": 0, "P2O5": 0, "K2O": 60},
    "NPK17": {"N": 17, "P2O5": 17, "K2O": 17},
}


def calculate_product_plan(fertilizers, area_ha=None, prices=None, *, area_acres=None):
    """Convert whole-plot product quantities (kg) to nutrient rates and KES cost.

    Supply exactly one area: hectares or acres. Grades are N-P2O5-K2O
    percentages, using a known product or an explicit grade dictionary.
    Prices use uppercase product keys and either price_kes_per_bag with
    bag_weight_kg, or price_kes_per_kg. An item's total_cost_kes overrides
    its catalog quote. Costs cover the supplied whole-plot quantity.
    """
    from app.services.feature_preparation_service import ACRE_TO_HA, number, object_input

    if not isinstance(fertilizers, list) or len(fertilizers) > 12:
        raise ValueError("fertilizers must be a list with at most 12 products")
    if (area_ha is None) == (area_acres is None):
        raise ValueError("Supply exactly one of area_ha or area_acres")
    if area_acres is not None:
        area_ha = number(area_acres, "area_acres") * ACRE_TO_HA
    if prices is None:
        prices = {}
    object_input(prices, "prices")
    area_ha = number(area_ha, "area_ha", 0.0001, 10000)
    nutrients = {"N": 0.0, "P": 0.0, "K": 0.0}
    products, total = [], 0.0
    pricing_methods = set()
    for item in fertilizers:
        object_input(item, "fertilizer")
        if not isinstance(item.get("product"), str) or not item["product"].strip():
            raise ValueError("Each fertilizer needs a product name")
        product = item["product"].strip().upper()
        quantity = number(item.get("quantity_kg"), f"{product}.quantity_kg", 0, 100000)
        grade = item.get("grade", PRODUCT_GRADES.get(product))
        object_input(grade, f"{product}.grade (N, P2O5, K2O percentages)")
        grade = {key: number(grade.get(key), f"{product}.{key}", 0, 100) for key in ("N", "P2O5", "K2O")}
        if sum(grade.values()) > 100:
            raise ValueError("Product nutrient percentages cannot sum to more than 100")
        bags_equivalent = None
        if "total_cost_kes" in item:
            cost = number(item["total_cost_kes"], f"{product}.total_cost_kes", 0, 100000000)
            method = "supplied_total_cost"
        elif quantity == 0 and product not in prices:
            cost = 0.0
            method = "zero_quantity"
        else:
            quote = object_input(prices.get(product), f"prices.{product}")
            if "price_kes_per_kg" in quote:
                if "price_kes_per_bag" in quote:
                    raise ValueError(f"prices.{product}: supply a price per kg or per bag, not both")
                price = number(quote["price_kes_per_kg"], f"{product}.price_kes_per_kg", 0, 1000000)
                cost = quantity * price
                method = "per_kg_cost"
            else:
                price = number(quote.get("price_kes_per_bag"), f"{product}.price_kes_per_bag", 0, 1000000)
                weight = number(quote.get("bag_weight_kg"), f"{product}.bag_weight_kg", 0.01, 1000)
                bags_equivalent = quantity / weight
                cost = bags_equivalent * price
                method = "proportional_bag_cost"
        nutrients["N"] += quantity * grade["N"] / 100 / area_ha
        nutrients["P"] += quantity * grade["P2O5"] / 100 / P_TO_P2O5 / area_ha
        nutrients["K"] += quantity * grade["K2O"] / 100 / K_TO_K2O / area_ha
        products.append({"product": product, "quantity_kg": quantity, "quantity_kg_ha": quantity / area_ha,
                         "grade": grade, "cost_kes": cost, "bags_equivalent": bags_equivalent,
                         "pricing_method": method})
        pricing_methods.add(method)
        total += cost
    pricing_method = (next(iter(pricing_methods)) if len(pricing_methods) == 1 else
                      "mixed_product_costs" if pricing_methods else "proportional_bag_cost")
    return {"products": products, "nutrients_kg_ha": nutrients, "cost_kes": total,
            "area_ha": area_ha, "cost_kes_ha": total / area_ha, "pricing_method": pricing_method,
            "nutrient_basis": "elemental_P_K_assumed_from_product_oxide_labels"}
