import secrets
import time

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

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
    # Keep a short-lived random token in Flask's signed session, never in a URL.
    issued = session.get("registration_csrf_issued", 0)
    if not session.get("registration_csrf") or not 0 <= time.time() - issued < 3600:
        session["registration_csrf"] = secrets.token_urlsafe(32)
        session["registration_csrf_issued"] = time.time()
    return session["registration_csrf"]


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
        expected = session.get("registration_csrf", "")
        issued = session.get("registration_csrf_issued", 0)
        valid_csrf = (
            bool(expected) and submitted.isascii() and len(submitted) == len(expected)
            and secrets.compare_digest(submitted, expected)
            and 0 <= time.time() - issued < 3600
        )
        if not valid_csrf:
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
