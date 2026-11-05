"""Trusted-relay ingress and shared admission contracts; no provider assumptions.

No deployed limiter is supplied here. Host infrastructure must install a trusted
adapter and authenticate provider traffic before forwarding it to this backend.
"""

import base64
import hmac
import ipaddress
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from flask import current_app, request

from app.services.ussd_pin_service import _load_keyring, _secret_forms


UNAVAILABLE = "END Verification could not be completed. Please try again later."


class CallbackRefused(Exception):
    """Fixed public status; never carries operational details or submitted data."""

    def __init__(self, status=503):
        super().__init__("USSD callback unavailable.")
        self.status = status


@dataclass(frozen=True)
class CallbackLimit:
    scope: str
    key: bytes = field(repr=False)
    count: int
    seconds: int
    burst: int


class SharedCallbackLimiter(ABC):
    """Trusted deployment adapter; no local-counter implementation is provided.

    admit() must atomically enforce all supplied sliding-window quotas plus
    token-bucket bursts (refill count/seconds), across every worker/replica.
    Return literal bool, without waiting; outage raises, and denial consumes
    none of the supplied quotas. Use backend time and bounded calls/key TTLs.
    """

    deployment_wide = False

    @abstractmethod
    def admit(self, limits):
        raise NotImplementedError


@dataclass(frozen=True)
class CallbackSettings:
    service_code: str
    metadata_key: bytes = field(repr=False)
    relay_secret: str = field(repr=False)
    relay_ips: tuple
    phone_count: int
    phone_burst: int
    sessions_count: int
    global_count: int
    global_burst: int
    session_seconds: int
    retention_seconds: int
    limiter: SharedCallbackLimiter = field(repr=False)


def _positive(name, maximum):
    value = current_app.config.get(name)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,7}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= maximum:
        raise CallbackRefused()
    return value


def _key(value):
    if not isinstance(value, str) or len(value) != 44:
        raise CallbackRefused()
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != 32 or base64.b64encode(decoded).decode("ascii") != value:
        raise CallbackRefused()
    return decoded


def load_callback_settings():
    """Validate only for the opt-in flow; failures do not disable web routes."""
    try:
        config = current_app.config
        if config.get("USSD_PERSISTENT_FLOW_ENABLED") is not True or current_app.debug:
            raise CallbackRefused()
        code = config.get("USSD_SERVICE_CODE")
        if not isinstance(code, str) or len(code) > 40 or not re.fullmatch(r"\*[0-9]{1,6}(?:\*[0-9]{1,6})*#", code):
            raise CallbackRefused()
        metadata = _key(config.get("USSD_METADATA_HMAC_KEY"))
        secret = config.get("USSD_RELAY_SECRET")
        relay_key = _key(secret)
        if not config.get("SECRET_KEY"):
            raise CallbackRefused()
        peppers, _ = _load_keyring()  # Configuration only: never performs scrypt.
        forbidden = [metadata] + list(peppers.values())
        for name in ("SECRET_KEY", "AFRICASTALKING_API_KEY", "AT_API_KEY", "AFRICAS_TALKING_API_KEY"):
            forbidden.extend(_secret_forms(config.get(name)))
        if (any(hmac.compare_digest(relay_key, other) for other in forbidden)
                or any(hmac.compare_digest(metadata, other) for other in _secret_forms(config.get("SECRET_KEY")))):
            raise CallbackRefused()
        raw_ips = config.get("USSD_TRUSTED_RELAY_IPS")
        if not isinstance(raw_ips, str) or not 1 <= len(raw_ips) <= 400:
            raise CallbackRefused()
        ips = tuple(ipaddress.ip_address(value.strip()) for value in raw_ips.split(","))
        if (not 1 <= len(ips) <= 8 or len(set(ips)) != len(ips)
                or any(ip.is_unspecified or ip.is_multicast for ip in ips)):
            raise CallbackRefused()
        limiter = current_app.extensions.get("ussd_shared_limiter")
        if not isinstance(limiter, SharedCallbackLimiter) or limiter.deployment_wide is not True:
            raise CallbackRefused()
        phone = _positive("USSD_PHONE_CALLBACKS_PER_MINUTE", 10000)
        phone_burst = _positive("USSD_PHONE_CALLBACK_BURST", phone)
        sessions = _positive("USSD_NEW_SESSIONS_PER_MINUTE", phone)
        global_count = _positive("USSD_GLOBAL_CALLBACKS_PER_SECOND", 10000)
        global_burst = _positive("USSD_GLOBAL_CALLBACK_BURST", global_count)
        lifetime = _positive("USSD_SESSION_SECONDS", 180)
        retention = _positive("USSD_RETENTION_SECONDS", 604800)
        if retention < lifetime:
            raise CallbackRefused()
        return CallbackSettings(code, metadata, secret, ips, phone, phone_burst, sessions,
                                global_count, global_burst, lifetime, retention, limiter)
    except Exception:
        raise CallbackRefused() from None


def verify_relay(settings):
    """Require native TLS, untouched socket identity, and a relay-only secret.

    ProxyFix rewriting of REMOTE_ADDR/scheme is refused. X-Forwarded-* and
    Origin are never consulted. Relay-to-backend TLS is required in this stage.
    """
    try:
        if (not request.is_secure or "werkzeug.proxy_fix.orig" in request.environ
                or ipaddress.ip_address(request.environ.get("REMOTE_ADDR", "")) not in settings.relay_ips):
            raise CallbackRefused(403)
        supplied = request.headers.get("X-FarmAdvis-USSD-Relay", "")
        if (not isinstance(supplied, str) or not supplied.isascii() or len(supplied) != 44
                or not hmac.compare_digest(supplied, settings.relay_secret)):
            raise CallbackRefused(403)
    except Exception:
        raise CallbackRefused(403) from None


def admit_callbacks(settings, phone_tag=None, *, initial=False):
    """Global admission precedes parsing; phone/initial quotas follow parsing."""
    if phone_tag is None:
        limits = (CallbackLimit("global", b"farmadvis/ussd/global/v1", settings.global_count, 1,
                                settings.global_burst),)
    else:
        limits = (CallbackLimit("phone", phone_tag, settings.phone_count, 60, settings.phone_burst),)
        if initial:
            limits += (CallbackLimit("initial", phone_tag, settings.sessions_count, 60, settings.sessions_count),)
    try:
        accepted = settings.limiter.admit(limits)
        if type(accepted) is not bool:
            raise CallbackRefused()
    except Exception:
        raise CallbackRefused() from None
    if not accepted:
        raise CallbackRefused(429)
