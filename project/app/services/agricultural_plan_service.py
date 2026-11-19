"""Transactional agricultural plans using the existing MySQL connection pattern.

Public operations require the authenticated farmer ID from the route, never
an owner ID supplied in JSON. Internal calculation services stay reusable.
"""

import logging
from contextlib import contextmanager

from mysql.connector import Error, IntegrityError

from app.db import get_db_connection
from app.models import agricultural_plan as model
from app.services.feature_preparation_service import number, object_input
from app.services.fertilizer_service import calculate_product_plan
from app.services.recommendation_service import compare_plot_plans
from app.services.roi_service import calculate_plot_roi


logger = logging.getLogger(__name__)
SEASONS = ("long_rains", "short_rains")
EDITABLE = {"variety", "farmgate_price_kes_kg", "seed_cost_kes", "labour_cost_kes",
            "other_cost_kes", "fertilizers", "prices"}


class PlanNotFound(Exception):
    """The requested plot or plan is absent or belongs to someone else."""


class DuplicateSeason(Exception):
    """The plot already has a plan for this year and season."""


def _positive_id(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _season(year, season):
    if isinstance(year, str) and year.isascii() and year.isdigit():
        year = int(year)
    if type(year) is not int or not 1000 <= year <= 9999:
        raise ValueError("planting_year must be a four-digit year")
    if not isinstance(season, str):
        raise ValueError("planting_season must be long_rains or short_rains")
    season = season.strip().lower().replace(" ", "_")
    if season not in SEASONS:
        raise ValueError("planting_season must be long_rains or short_rains")
    return year, season


@contextmanager
def _transaction():
    connection = get_db_connection()
    cursor = None
    try:
        connection.start_transaction()
        cursor = connection.cursor(dictionary=True)
        yield cursor
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except Error:
            logger.error("Agricultural plan rollback failed.")
        raise
    finally:
        for resource in (cursor, connection):
            if resource is not None:
                try:
                    resource.close()
                except Error:
                    logger.warning("Agricultural plan resource cleanup failed.")


def _owned_plan(cursor, farmer_id, plan_id):
    _positive_id(farmer_id, "farmer_id")
    _positive_id(plan_id, "plan_id")
    plan = model.find_plan(cursor, farmer_id, plan_id)
    if plan is None:
        raise PlanNotFound("Plan not found")
    return plan


def _owned_plot(cursor, farmer_id, plot_id):
    _positive_id(farmer_id, "farmer_id")
    _positive_id(plot_id, "plot_id")
    plot = model.get_plot(cursor, farmer_id, plot_id)
    if plot is None:
        raise PlanNotFound("Plot not found")
    return plot


def _validate_plan(data, area_ha):
    object_input(data, "plan")
    unknown = set(data) - (EDITABLE | {"planting_year", "planting_season"})
    if unknown:
        raise ValueError("Plan contains unsupported fields")
    year, season = _season(data.get("planting_year"), data.get("planting_season"))
    if not isinstance(data.get("variety"), str) or data["variety"] not in ("623A", "HAC"):
        raise ValueError("variety must be exactly 623A or HAC")
    values = {"planting_year": year, "planting_season": season, "variety": data["variety"]}
    for name in ("farmgate_price_kes_kg", "seed_cost_kes", "labour_cost_kes", "other_cost_kes"):
        values[name] = number(data.get(name), name)
    prices = data.get("prices", {})
    calculated = calculate_product_plan(data.get("fertilizers"), area_ha=area_ha, prices=prices)
    # Validate representable whole-plot costs without inventing a yield estimate.
    calculate_plot_roi(0, values["farmgate_price_kes_kg"], calculated["cost_kes"],
                       values["seed_cost_kes"], values["labour_cost_kes"], values["other_cost_kes"],
                       area_ha=area_ha)
    products = []
    for product in calculated["products"]:
        if len(product["product"]) > 100:
            raise ValueError("Product names must be at most 100 characters")
        method = product["pricing_method"]
        if method == "proportional_bag_cost":
            quote = prices[product["product"]]
            pricing = {key: float(quote[key]) for key in ("price_kes_per_bag", "bag_weight_kg")}
        elif method == "per_kg_cost":
            pricing = {"price_kes_per_kg": float(prices[product["product"]]["price_kes_per_kg"])}
        else:
            pricing = {"total_cost_kes": product["cost_kes"]}
        products.append({"product": product["product"], "quantity_kg": product["quantity_kg"],
                         "grade": product["grade"], "pricing": pricing, "cost_kes": product["cost_kes"]})
    return values, products


def _plan_input(plan):
    data = {name: plan[name] for name in model.PLAN_COLUMNS}
    data.update(fertilizers=[], prices={})
    for product in plan["fertilizers"]:
        item = {key: product[key] for key in ("product", "grade", "quantity_kg")}
        if "total_cost_kes" in product["pricing"]:
            item["total_cost_kes"] = product["pricing"]["total_cost_kes"]
        else:
            data["prices"][product["product"]] = product["pricing"]
        data["fertilizers"].append(item)
    return data


def _compare(plan):
    data = _plan_input(plan)
    return compare_plot_plans({
        "plot": {"area_ha": plan["area_ha"]},
        "management": {"variety": plan["variety"], "fertilizers": data["fertilizers"]},
        "prices": data["prices"], "farmgate_price_kes_kg": plan["farmgate_price_kes_kg"],
        "costs": {"seed_kes": plan["seed_cost_kes"], "labour_kes": plan["labour_cost_kes"],
                  "other_kes": plan["other_cost_kes"]},
    })


def create_plan(farmer_id, plot_id, data):
    with _transaction() as cursor:
        plot = _owned_plot(cursor, farmer_id, plot_id)
        values, products = _validate_plan(data, float(plot["area_ha"]))
        if model.find_season(cursor, plot_id, values["planting_year"], values["planting_season"]):
            raise DuplicateSeason("A plan already exists for this plot, year and season")
        try:
            plan_id = model.insert_plan(cursor, plot_id, values)
        except IntegrityError as exc:
            if exc.errno == 1062:
                raise DuplicateSeason("A plan already exists for this plot, year and season") from exc
            raise
        model.replace_products(cursor, plan_id, products)
        return _owned_plan(cursor, farmer_id, plan_id)


def get_plan(farmer_id, plan_id):
    with _transaction() as cursor:
        return _owned_plan(cursor, farmer_id, plan_id)


def list_plans(farmer_id, plot_id):
    with _transaction() as cursor:
        _owned_plot(cursor, farmer_id, plot_id)
        return [_owned_plan(cursor, farmer_id, plan_id) for plan_id in model.list_plan_ids(cursor, plot_id)]


def get_season_plan(farmer_id, plot_id, planting_year, planting_season):
    year, season = _season(planting_year, planting_season)
    with _transaction() as cursor:
        _owned_plot(cursor, farmer_id, plot_id)
        row = model.find_season(cursor, plot_id, year, season)
        if row is None:
            raise PlanNotFound("Plan not found")
        return _owned_plan(cursor, farmer_id, row["plan_id"])


def update_plan(farmer_id, plan_id, changes):
    object_input(changes, "changes")
    if not changes or set(changes) - EDITABLE:
        raise ValueError("Supply editable plan fields only; plot, year and season cannot be changed")
    with _transaction() as cursor:
        existing = _owned_plan(cursor, farmer_id, plan_id)
        data = {**_plan_input(existing), **changes}
        values, products = _validate_plan(data, existing["area_ha"])
        model.update_plan(cursor, plan_id, values)
        model.replace_products(cursor, plan_id, products)
        model.clear_selection(cursor, plan_id)
        return _owned_plan(cursor, farmer_id, plan_id)


def delete_plan(farmer_id, plan_id):
    with _transaction() as cursor:
        _owned_plan(cursor, farmer_id, plan_id)
        model.delete_plan(cursor, plan_id)
    return {"deleted_plan_id": plan_id}


def compare_saved_plan(farmer_id, plan_id):
    with _transaction() as cursor:
        plan = _owned_plan(cursor, farmer_id, plan_id)
        return {"plan_id": plan_id, **_compare(plan)}


def select_scenario(farmer_id, plan_id, scenario_id):
    if not isinstance(scenario_id, str):
        raise ValueError("scenario_id must identify an evaluated option")
    with _transaction() as cursor:
        plan = _owned_plan(cursor, farmer_id, plan_id)
        scenario = next((row for row in _compare(plan)["scenarios"] if row["scenario_id"] == scenario_id), None)
        if scenario is None or scenario["status"] != "available":
            raise ValueError("Select an available scenario evaluated for this plan")
        model.save_selection(cursor, plan_id, scenario)
        return _owned_plan(cursor, farmer_id, plan_id)["selection"]
