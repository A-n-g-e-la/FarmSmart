"""Farmer-owned plan endpoints using existing session and CSRF protection."""

from flask import Blueprint, g, jsonify, request
from mysql.connector import Error

from app.security import csrf_token, farmer_required, valid_csrf
from app.services import agricultural_plan_service as plans


plans_bp = Blueprint("agricultural_plans", __name__, url_prefix="/farmer")


@plans_bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def _body():
    if not valid_csrf("agricultural_plans", request.headers.get("X-CSRF-Token", "")):
        return None, (jsonify(ok=False, code="csrf_error"), 403)
    if request.content_length and request.content_length > 65536:
        return None, (jsonify(ok=False, code="validation_error", message="Request too large"), 413)
    if not request.is_json:
        return None, (jsonify(ok=False, code="validation_error", message="Send JSON"), 415)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify(ok=False, code="validation_error", message="Provide an object"), 400)
    return data, None


def _call(function, *args, status=200):
    try:
        return jsonify(ok=True, result=function(g.farmer_identity["farmer_id"], *args)), status
    except plans.PlanNotFound:
        return jsonify(ok=False, code="not_found", message="Plot or plan not found"), 404
    except plans.DuplicateSeason:
        return jsonify(ok=False, code="duplicate_season", message="A plan already exists for that season"), 409
    except ValueError as exc:
        return jsonify(ok=False, code="validation_error", message=str(exc)), 400
    except Error:
        return jsonify(ok=False, code="database_error", message="The operation could not be completed"), 503


@plans_bp.get("/agricultural-plans/csrf")
@farmer_required
def plan_csrf():
    return jsonify(ok=True, csrf_token=csrf_token("agricultural_plans"))


@plans_bp.route("/plots/<int:plot_id>/agricultural-plans", methods=["GET", "POST"])
@farmer_required
def plot_plans(plot_id):
    if request.method == "GET":
        if "year" in request.args or "season" in request.args:
            return _call(plans.get_season_plan, plot_id, request.args.get("year"), request.args.get("season"))
        return _call(plans.list_plans, plot_id)
    data, error = _body()
    return error if error is not None else _call(plans.create_plan, plot_id, data, status=201)


@plans_bp.route("/agricultural-plans/<int:plan_id>", methods=["GET", "PATCH", "DELETE"])
@farmer_required
def plan_detail(plan_id):
    if request.method == "GET":
        return _call(plans.get_plan, plan_id)
    # DELETE also requires an object body and a header token.
    data, error = _body()
    if error is not None:
        return error
    if request.method == "DELETE":
        return _call(plans.delete_plan, plan_id)
    return _call(plans.update_plan, plan_id, data)


@plans_bp.get("/agricultural-plans/<int:plan_id>/comparisons")
@farmer_required
def compare_plan(plan_id):
    return _call(plans.compare_saved_plan, plan_id)


@plans_bp.put("/agricultural-plans/<int:plan_id>/selection")
@farmer_required
def choose_plan_option(plan_id):
    data, error = _body()
    if error is not None:
        return error
    if set(data) != {"scenario_id"}:
        return jsonify(ok=False, code="validation_error", message="Supply scenario_id only"), 400
    return _call(plans.select_scenario, plan_id, data["scenario_id"])
