import time
from collections.abc import Mapping

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

from app.security import csrf_token, farmer_required, valid_csrf
from app.services.authentication_service import authenticate_farmer
from app.services.registration_service import register_farmer


farmer_bp = Blueprint("farmer", __name__, url_prefix="/farmer")


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


@farmer_bp.get("/login-page")
def login_page():
    # Keep HTML delivery separate from the existing JSON token/authentication API.
    return render_template("farmer/login.html"), 200, {"Cache-Control": "no-store"}


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
