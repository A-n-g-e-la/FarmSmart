import json
import time
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

from app.security import csrf_token, farmer_required, valid_csrf
from app.services.authentication_service import authenticate_farmer
from app.services.registration_service import register_farmer
from app.services.farm_plot_service import (
    get_farmer_plots, get_farmer_plot, register_farmer_plot, update_farmer_plot, delete_farmer_plot,
)
from app.config.plot_options import PLOT_OPTIONS


farmer_bp = Blueprint("farmer", __name__, url_prefix="/farmer")


@farmer_bp.get("/plots")
@farmer_required
def plots():
    # Ignore client IDs in query strings, headers, or bodies; use revalidated identity.
    result = get_farmer_plots(g.farmer_identity["farmer_id"])
    return jsonify(result), 200 if result["ok"] else 503


@farmer_bp.get("/plots/csrf-token")
@farmer_required
def plot_csrf_token():
    return jsonify({"ok": True, "csrf_token": csrf_token("plot_registration")})


@farmer_bp.post("/plots")
@farmer_required
def create_plot():
    if request.is_json:
        try:
            # Preserve submitted decimal precision instead of rounding via float.
            data = json.loads(request.get_data(), parse_float=Decimal)
        except (ValueError, UnicodeError, InvalidOperation):
            data = None
    elif request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        data = request.form
    else:
        data = None
    if not isinstance(data, Mapping):
        return jsonify({"ok": False, "code": "validation_error", "errors": {
            "request": "Send a plot object as JSON or form data.",
        }}), 400
    token = request.headers.get("X-CSRF-Token") or data.get("csrf_token", "")
    if not valid_csrf("plot_registration", token):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 403
    result = register_farmer_plot(g.farmer_identity["farmer_id"], data)
    status = 201 if result["ok"] else 400 if result["code"] == "validation_error" else 503
    return jsonify(result), status


@farmer_bp.patch("/plots/<int:plot_id>")
@farmer_required
def update_plot(plot_id):
    # PATCH is a JSON object. Keep the CSRF token in its header so the payload
    # contains only editable fields and rejects every unknown property.
    data = None
    if request.is_json:
        try:
            data = json.loads(request.get_data(), parse_float=Decimal)
        except (ValueError, UnicodeError, InvalidOperation):
            pass
    if not isinstance(data, Mapping):
        return jsonify({"ok": False, "code": "validation_error", "errors": {
            "request": "Send a plot object as JSON.",
        }}), 400
    if not valid_csrf("plot_registration", request.headers.get("X-CSRF-Token", "")):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 403
    result = update_farmer_plot(g.farmer_identity["farmer_id"], plot_id, data)
    status = 200 if result["ok"] else {
        "validation_error": 400, "not_found": 404,
    }.get(result["code"], 503)
    return jsonify(result), status


@farmer_bp.after_request
def plot_cache_control(response):
    if request.path.startswith("/farmer/plots"):
        response.headers["Cache-Control"] = "no-store"
    return response


@farmer_bp.get("/plots/options")
@farmer_required
def plot_options():
    return jsonify({"ok": True, "options": PLOT_OPTIONS})


@farmer_bp.get("/plots/<int:plot_id>")
@farmer_required
def plot_detail(plot_id):
    result = get_farmer_plot(g.farmer_identity["farmer_id"], plot_id)
    return jsonify(result), 200 if result["ok"] else 404 if result["code"] == "not_found" else 503


@farmer_bp.delete("/plots/<int:plot_id>")
@farmer_required
def delete_plot(plot_id):
    if not valid_csrf("plot_registration", request.headers.get("X-CSRF-Token", "")):
        return jsonify({"ok": False, "code": "csrf_error"}), 403
    result = delete_farmer_plot(g.farmer_identity["farmer_id"], plot_id)
    status = 200 if result["ok"] else {"not_found": 404, "plot_has_plans": 409}.get(result["code"], 503)
    return jsonify(result), status



@farmer_bp.post("/register")
def register():
    # Keep the existing JSON-only endpoint separate from browser form submissions.
    if not request.is_json:
        return jsonify({"ok": False, "code": "validation_error", "errors": {
            "request": "Send a JSON object with Content-Type application/json."
        }}), 415
    result = register_farmer(request.get_json(silent=True))
    # Map safe service results to predictable HTTP responses without auto-login.
    if result["ok"]:
        return jsonify(result), 201
    # Unknown service failures must never fall back to an input-validation response.
    status = {
        "validation_error": 400,
        "duplicate_phone": 409,
        "database_error": 503,
        "internal_error": 500,
    }.get(result["code"], 500)
    return jsonify(result), status


def _form_csrf_token():
    # Preserve registration token names and expiry through the shared helper.
    return csrf_token("registration")


@farmer_bp.route("/registration", methods=["GET", "POST"])
def registration_form():
    values = {}
    errors = {}
    status = 200
    if request.method == "POST":
        # Only allowlisted, non-sensitive fields may be passed back to the template.
        values = {field: request.form.get(field, "") for field in (
            "first_name", "last_name", "phone_number", "location"
        )}
        submitted = request.form.get("csrf_token", "")
        if not valid_csrf("registration", submitted):
            errors["request"] = "Your form expired or could not be verified. Please try again."
            status = 400
        else:
            # Reuse all validation, hashing, and transaction handling from the service.
            result = register_farmer(request.form)
            if result["ok"]:
                session.pop("registration_csrf", None)
                session.pop("registration_csrf_issued", None)
                flash("Your farmer account was created successfully. You have not been logged in.", "success")
                # Redirect to a GET so refreshing the success page cannot repeat INSERTs.
                return redirect(url_for("farmer.registration_form"), code=303)
            errors = result["errors"]
            status = {
                "validation_error": 400, "duplicate_phone": 409,
                "database_error": 503, "internal_error": 500,
            }.get(result["code"], 500)
    response = render_template(
        "farmer/register.html", values=values, errors=errors,
        csrf_token=_form_csrf_token(),
    )
    # Registration pages contain contact details and session-specific CSRF tokens.
    return response, status, {"Cache-Control": "no-store"}


@farmer_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        # Phase 3 can use this token in a form field or the X-CSRF-Token header.
        return jsonify({"ok": True, "csrf_token": csrf_token("login")}), 200, {"Cache-Control": "no-store"}
    if request.is_json:
        data = request.get_json(silent=True)
    elif request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        data = request.form
    else:
        return jsonify({"ok": False, "code": "validation_error", "message": "Send JSON or form data."}), 415
    if not isinstance(data, Mapping):
        return jsonify({"ok": False, "code": "validation_error", "message": "Provide a credentials object."}), 400
    token = request.headers.get("X-CSRF-Token") or data.get("csrf_token", "")
    if not valid_csrf("login", token):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 400
    # Discard previous identities and tokens before attempting account switching.
    session.clear()
    result = authenticate_farmer(data.get("phone_number"), data.get("password"))
    if not result["ok"]:
        result = dict(result, csrf_token=csrf_token("login"))
        return jsonify(result), 401 if result["code"] == "invalid_credentials" else 503, {"Cache-Control": "no-store"}
    session.permanent = True
    session["user_id"] = result["identity"]["user_id"]
    session["farmer_id"] = result["identity"]["farmer_id"]
    session["authenticated_at"] = time.time()
    return jsonify(dict(result, csrf_token=csrf_token("logout"))), 200, {"Cache-Control": "no-store"}


@farmer_bp.get("/home")
@farmer_required
def home():
    return jsonify({"ok": True, "message": "Farmer authentication confirmed.",
                    "identity": g.farmer_identity, "csrf_token": csrf_token("logout")}), 200, {"Cache-Control": "no-store"}


@farmer_bp.post("/logout")
@farmer_required
def logout():
    data = request.get_json(silent=True) if request.is_json else request.form
    token = request.headers.get("X-CSRF-Token") or (data.get("csrf_token", "") if isinstance(data, Mapping) else "")
    if not valid_csrf("logout", token):
        return jsonify({"ok": False, "code": "csrf_error", "message": "The request could not be verified."}), 400
    session.clear()
    return jsonify({"ok": True, "message": "Logged out successfully."}), 200, {"Cache-Control": "no-store"}
