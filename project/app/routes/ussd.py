import re

from flask import Blueprint, Response, current_app, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from app.services.registration_service import normalize_kenyan_phone
from app.models.ussd_fingerprints import fingerprint_phone
from app.services.ussd_callback_security import (
    CallbackRefused, UNAVAILABLE, admit_callbacks, load_callback_settings, verify_relay,
)
from app.services.ussd_callback_service import orchestrate_callback


ussd_bp = Blueprint("ussd", __name__)

MAX_CALLBACK_BYTES = 4096
CALLBACK_FIELDS = {"sessionId", "serviceCode", "phoneNumber", "text"}
OPTIONAL_CALLBACK_FIELDS = {"networkCode"}
INVALID_REQUEST = "END Invalid request. Please dial again."
WELCOME = "CON Welcome to FarmAdvis\n1. Request maize advice\n0. Exit"


def _reply(message, status=200):
    return Response(message, status=status, content_type="text/plain; charset=utf-8")


@ussd_bp.after_request
def prevent_callback_caching(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@ussd_bp.errorhandler(BadRequest)
def malformed_callback(error):
    return _reply(INVALID_REQUEST, 400)


@ussd_bp.errorhandler(RequestEntityTooLarge)
def oversized_callback(error):
    return _reply("END Request too large. Please dial again.", 413)


@ussd_bp.post("/ussd")
def callback():
    if current_app.config.get("USSD_PERSISTENT_FLOW_ENABLED", False) is False:
        return _callback_body()
    try:
        settings = load_callback_settings()
        verify_relay(settings)
        admit_callbacks(settings)  # Before reading the body; no DB or hashing.
        return _callback_body(settings)
    except CallbackRefused as error:
        return _reply(UNAVAILABLE, error.status)
    except (BadRequest, RequestEntityTooLarge):
        raise  # Existing fixed plain-text blueprint handlers.
    except Exception:
        return _reply(UNAVAILABLE, 503)


def _callback_body(settings=None):
    # Limits apply only to this callback, preserving existing web request limits.
    # One extra byte lets us detect an oversized stream without Content-Length.
    request.max_content_length = MAX_CALLBACK_BYTES + 1
    request.max_form_memory_size = MAX_CALLBACK_BYTES
    if request.content_length is not None and request.content_length > MAX_CALLBACK_BYTES:
        raise RequestEntityTooLarge()
    if request.mimetype != "application/x-www-form-urlencoded":
        return _reply(INVALID_REQUEST, 415)
    if len(request.get_data()) > MAX_CALLBACK_BYTES:
        raise RequestEntityTooLarge()

    form = request.form
    # networkCode is ignored, untrusted provider metadata, never an identity signal.
    # Reject missing, unexpected, or ambiguous fields. Empty text starts a session.
    if (not CALLBACK_FIELDS.issubset(form)
            or not set(form).issubset(CALLBACK_FIELDS | OPTIONAL_CALLBACK_FIELDS)
            or any(len(form.getlist(key)) != 1 for key in form)):
        return _reply(INVALID_REQUEST, 400)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", form["sessionId"]):
        return _reply(INVALID_REQUEST, 400)
    code = form["serviceCode"]
    if len(code) > 40 or not re.fullmatch(r"\*[0-9]{1,6}(?:\*[0-9]{1,6})*#", code):
        return _reply(INVALID_REQUEST, 400)
    if normalize_kenyan_phone(form["phoneNumber"]) is None:
        return _reply(INVALID_REQUEST, 400)
    text = form["text"]
    if (len(text) > 128 or text.count("*") > 15
            or (text and not re.fullmatch(r"[0-9]+(?:\*[0-9]+)*", text))):
        return _reply(INVALID_REQUEST, 400)

    if settings is not None:
        if code != settings.service_code:
            return _reply(INVALID_REQUEST, 400)
        phone = normalize_kenyan_phone(form["phoneNumber"])
        admit_callbacks(settings, fingerprint_phone(settings.metadata_key, phone), initial=text == "")
        return _reply(orchestrate_callback(settings, form["sessionId"], code, phone, text))

    # This shell neither verifies callback origin nor authenticates the caller.
    # Never infer authorization from the phone, session ID, or input history.
    if text == "":
        return _reply(WELCOME)
    if text == "0":
        return _reply("END Thank you for using FarmAdvis.")
    if text == "1":
        return _reply("END Account verification is not yet available. Please try again later.")
    return _reply("END Invalid selection. Please dial again.")
