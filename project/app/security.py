import secrets
import time
from functools import wraps

from flask import current_app, g, jsonify, session

from app.services.authentication_service import get_active_farmer_identity
from app.services.officer_authentication_service import get_active_officer_identity


def csrf_token(scope):
    # Scope tokens so registration, login, and logout cannot substitute for each other.
    key = scope + "_csrf"
    issued = session.get(key + "_issued", 0)
    if not session.get(key) or not 0 <= time.time() - issued < 3600:
        session[key] = secrets.token_urlsafe(32)
        session[key + "_issued"] = time.time()
    return session[key]


def valid_csrf(scope, submitted):
    expected = session.get(scope + "_csrf", "")
    issued = session.get(scope + "_csrf_issued", 0)
    return (
        isinstance(submitted, str) and bool(expected) and submitted.isascii()
        and len(submitted) == len(expected)
        and secrets.compare_digest(submitted, expected)
        and 0 <= time.time() - issued < 3600
    )


def farmer_required(view):
    @wraps(view)
    def protected(*args, **kwargs):
        user_id = session.get("user_id")
        farmer_id = session.get("farmer_id")
        issued = session.get("authenticated_at")
        lifetime = current_app.permanent_session_lifetime.total_seconds()
        # Reject malformed identities and enforce absolute expiry, not sliding expiry.
        valid_identity = (
            type(user_id) is int and user_id > 0
            and type(farmer_id) is int and farmer_id > 0
            and type(issued) in (int, float)
            and 0 <= time.time() - issued < lifetime
        )
        if not valid_identity:
            session.clear()
            return jsonify({"ok": False, "code": "unauthenticated", "message": "Please log in."}), 401
        result = get_active_farmer_identity(user_id, farmer_id)
        if not result["ok"]:
            if result["code"] == "authentication_error":
                # Deny access on outages without confusing them with invalid credentials.
                return jsonify({"ok": False, "code": "authentication_error", "message": "Authentication could not be completed. Try again later."}), 503
            session.clear()
            return jsonify({"ok": False, "code": "unauthenticated", "message": "Please log in."}), 401
        g.farmer_identity = result["identity"]
        return view(*args, **kwargs)
    return protected


def officer_required(view):
    @wraps(view)
    def protected(*args, **kwargs):
        user_id = session.get("user_id")
        officer_id = session.get("agricultural_officer_id")
        issued = session.get("authenticated_at")
        lifetime = current_app.permanent_session_lifetime.total_seconds()
        valid_identity = (
            type(user_id) is int and user_id > 0
            and type(officer_id) is int and officer_id > 0
            and "farmer_id" not in session
            and type(issued) in (int, float)
            and 0 <= time.time() - issued < lifetime
        )
        if not valid_identity:
            session.clear()
            return jsonify({"ok": False, "code": "unauthenticated", "message": "Please log in."}), 401
        result = get_active_officer_identity(user_id, officer_id)
        if not result["ok"]:
            if result["code"] == "authentication_error":
                return jsonify({"ok": False, "code": "authentication_error", "message": "Authentication could not be completed. Try again later."}), 503
            session.clear()
            return jsonify({"ok": False, "code": "unauthenticated", "message": "Please log in."}), 401
        g.officer_identity = result["identity"]
        return view(*args, **kwargs)
    return protected
