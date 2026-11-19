import time
from collections.abc import Mapping

from flask import Blueprint, g, jsonify, request, session

from app.security import csrf_token, officer_required, valid_csrf
from app.services.officer_authentication_service import authenticate_officer
from app.services.officer_registration_service import register_officer
from app.services.officer_farmer_service import get_assigned_farmers, get_assigned_farmer_plots, get_assigned_farmer_plans


officer_bp = Blueprint("officer", __name__, url_prefix="/officer")


@officer_bp.after_request
def officer_cache_control(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@officer_bp.get("/dashboard")
@officer_required
def dashboard():
    result = get_assigned_farmers(g.officer_identity["agricultural_officer_id"])
    return jsonify(result), 200 if result["ok"] else 503


@officer_bp.get("/api/farmers")
@officer_required
def assigned_farmers():
    result = get_assigned_farmers(g.officer_identity["agricultural_officer_id"])
    return jsonify(result), 200 if result["ok"] else 503


@officer_bp.get("/api/farmers/<int:farmer_id>/plots")
@officer_required
def assigned_farmer_plots(farmer_id):
    result = get_assigned_farmer_plots(g.officer_identity["agricultural_officer_id"], farmer_id)
    return jsonify(result), 200 if result["ok"] else 404 if result["code"] == "not_found" else 503


@officer_bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return jsonify({"ok": True, "csrf_token": csrf_token("officer_registration")})
    if not request.is_json:
        return jsonify({"ok": False, "code": "validation_error", "errors": {
            "request": "Send a JSON object with Content-Type application/json.",
        }}), 415
    data = request.get_json(silent=True)
    if not isinstance(data, Mapping):
        return jsonify({"ok": False, "code": "validation_error", "errors": {
            "request": "Provide a JSON object.",
        }}), 400
    if not valid_csrf("officer_registration", request.headers.get("X-CSRF-Token", "")):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 403
    result = register_officer(data)
    if result["ok"]:
        session.pop("officer_registration_csrf", None)
        session.pop("officer_registration_csrf_issued", None)
    status = 201 if result["ok"] else {
        "validation_error": 400, "duplicate_email": 409, "database_error": 503,
    }.get(result["code"], 500)
    return jsonify(result), status


@officer_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return jsonify({"ok": True, "csrf_token": csrf_token("officer_login")})
    if request.is_json:
        data = request.get_json(silent=True)
    elif request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        data = request.form
    else:
        return jsonify({"ok": False, "code": "validation_error", "message": "Send JSON or form data."}), 415
    if not isinstance(data, Mapping):
        return jsonify({"ok": False, "code": "validation_error", "message": "Provide a credentials object."}), 400
    token = request.headers.get("X-CSRF-Token") or data.get("csrf_token", "")
    if not valid_csrf("officer_login", token):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 400
    # Match farmer account-switching semantics: no prior identity survives a
    # CSRF-verified login attempt, including an unsuccessful attempt.
    session.clear()
    result = authenticate_officer(data.get("email"), data.get("password"))
    if not result["ok"]:
        result = dict(result, csrf_token=csrf_token("officer_login"))
        return jsonify(result), 401 if result["code"] == "invalid_credentials" else 503
    session.permanent = True
    session["user_id"] = result["identity"]["user_id"]
    session["agricultural_officer_id"] = result["identity"]["agricultural_officer_id"]
    session["authenticated_at"] = time.time()
    return jsonify(dict(result, csrf_token=csrf_token("officer_logout")))


@officer_bp.get("/home")
@officer_required
def home():
    # Minimal identity response; assigned farmer data is available on the dashboard API.
    return jsonify({"ok": True, "identity": g.officer_identity,
                    "csrf_token": csrf_token("officer_logout")})


@officer_bp.post("/logout")
@officer_required
def logout():
    data = request.get_json(silent=True) if request.is_json else request.form
    token = request.headers.get("X-CSRF-Token") or (data.get("csrf_token", "") if isinstance(data, Mapping) else "")
    if not valid_csrf("officer_logout", token):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 400
    session.clear()
    return jsonify({"ok": True, "message": "Logged out successfully."})


@officer_bp.get("/api/farmers/<int:farmer_id>/plans")
@officer_required
def assigned_farmer_plans(farmer_id):
    result = get_assigned_farmer_plans(g.officer_identity["agricultural_officer_id"], farmer_id)
    return jsonify(result), 200 if result["ok"] else 404 if result["code"] == "not_found" else 503


@officer_bp.get("/api/farmers/<int:farmer_id>/plots/<int:plot_id>/plans")
@officer_required
def assigned_plot_plans(farmer_id, plot_id):
    result = get_assigned_farmer_plans(g.officer_identity["agricultural_officer_id"], farmer_id, plot_id=plot_id)
    return jsonify(result), 200 if result["ok"] else 404 if result["code"] == "not_found" else 503


@officer_bp.get("/api/farmers/<int:farmer_id>/plans/<int:plan_id>")
@officer_required
def assigned_plan(farmer_id, plan_id):
    result = get_assigned_farmer_plans(g.officer_identity["agricultural_officer_id"], farmer_id, plan_id=plan_id)
    return jsonify(result), 200 if result["ok"] else 404 if result["code"] == "not_found" else 503
