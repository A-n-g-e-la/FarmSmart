from collections.abc import Mapping

from flask import Blueprint, jsonify, render_template, request, session

from app.security import csrf_token, valid_csrf
from app.services.officer_registration_service import register_officer


officer_bp = Blueprint("officer", __name__, url_prefix="/officer")


@officer_bp.after_request
def officer_cache_control(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@officer_bp.get("/register-page")
def registration_page():
    return render_template("officer/register.html")


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
