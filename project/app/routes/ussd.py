import re

from flask import Blueprint, Response, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from app.services.registration_service import normalize_kenyan_phone


ussd_bp = Blueprint("ussd", __name__)

MAX_CALLBACK_BYTES = 4096
CALLBACK_FIELDS = {"sessionId", "serviceCode", "phoneNumber", "text"}
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
    # Reject missing, unexpected, or ambiguous fields. Empty text starts a session.
    if set(form) != CALLBACK_FIELDS or any(len(form.getlist(key)) != 1 for key in form):
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

    # This shell neither verifies callback origin nor authenticates the caller.
    # Never infer authorization from the phone, session ID, or input history.
    if text == "":
        return _reply(WELCOME)
    if text == "0":
        return _reply("END Thank you for using FarmAdvis.")
    if text == "1":
        return _reply("END Account verification is not yet available. Please try again later.")
    return _reply("END Invalid selection. Please dial again.")
