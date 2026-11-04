"""Domain-separated HMACs. Callers supply a stable, external metadata secret.

This key must differ from both the PIN pepper and Flask SECRET_KEY. These
helpers do not configure secrets, authenticate callers, or retain raw input.
"""

import hashlib
import hmac
import re


def _fingerprint(secret, domain, *parts):
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("A separate metadata key of at least 32 bytes is required.")
    message = bytearray(domain + b"\x00")
    for part in parts:
        message.extend(len(part).to_bytes(4, "big"))
        message.extend(part)
    return hmac.digest(secret, message, hashlib.sha256)


def derive_session_key(secret, service_code, provider_session_id):
    if (not isinstance(service_code, str) or len(service_code) > 40
            or not re.fullmatch(r"\*[0-9]{1,6}(?:\*[0-9]{1,6})*#", service_code)
            or not isinstance(provider_session_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", provider_session_id)):
        raise ValueError("Invalid session binding.")
    return _fingerprint(secret, b"farmadvis/ussd/session/v1",
                        service_code.encode("ascii"), provider_session_id.encode("ascii"))


def fingerprint_phone(secret, normalized_phone):
    if not isinstance(normalized_phone, str) or not re.fullmatch(r"\+254[17][0-9]{8}", normalized_phone):
        raise ValueError("A normalized Kenyan mobile number is required.")
    return _fingerprint(secret, b"farmadvis/ussd/phone/v1", normalized_phone.encode("ascii"))


def fingerprint_input(secret, session_key, phone_tag, cumulative_text):
    if (not isinstance(session_key, bytes) or len(session_key) != 32
            or not isinstance(phone_tag, bytes) or len(phone_tag) != 32
            or not isinstance(cumulative_text, str) or len(cumulative_text) > 128
            or cumulative_text.count("*") > 15
            or (cumulative_text and not re.fullmatch(r"[0-9]+(?:\*[0-9]+)*", cumulative_text))):
        raise ValueError("Invalid callback fingerprint input.")
    return _fingerprint(secret, b"farmadvis/ussd/input/v1", session_key,
                        phone_tag, cumulative_text.encode("ascii"))
