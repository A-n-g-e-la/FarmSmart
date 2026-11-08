"""Bounded admission for one serving process; never a shared-limiter adapter."""

from collections import deque
from dataclasses import dataclass
import math
import secrets
from threading import Lock
import time

from flask import current_app

from app.models.ussd_fingerprints import fingerprint_phone
from app.services.ussd_callback_security import CallbackLimit, CallbackRefused, _positive


@dataclass
class _Window:
    accepted: deque
    tokens: float
    updated: float
    expires: float
    policy: tuple


class LocalUssdLimiter:
    """Sliding windows plus token buckets, atomic within a single process.

    Restarts reset quotas. Every Flask app has one instance, shared by its
    threads. It deliberately cannot satisfy SharedCallbackLimiter's contract.
    """

    deployment_wide = False

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = Lock()
        self._entries = {}
        self._last_time = None
        # Volatile key for volatile counters only, never a persistent PIN secret.
        self._key = secrets.token_bytes(32)

    def phone_tag(self, normalized_phone):
        return fingerprint_phone(self._key, normalized_phone)

    def admit(self, limits, *, max_keys=4096):
        """Check all quotas before consuming any. Full state fails closed."""
        if type(max_keys) is not int or not 1 <= max_keys <= 20000 or not limits:
            raise ValueError("Invalid limiter configuration.")
        identities = []
        for limit in limits:
            if (not isinstance(limit, CallbackLimit) or not isinstance(limit.key, bytes)
                    or not 1 <= len(limit.key) <= 64 or limit.scope not in ("global", "phone", "initial")
                    or type(limit.count) is not int or not 1 <= limit.count <= 10000
                    or type(limit.seconds) is not int or not 1 <= limit.seconds <= 60
                    or type(limit.burst) is not int or not 1 <= limit.burst <= limit.count):
                raise ValueError("Invalid limiter configuration.")
            identities.append((limit.scope, limit.key))
        if len(set(identities)) != len(identities):
            raise ValueError("Invalid limiter configuration.")
        with self._lock:
            now = self._clock()
            if not math.isfinite(now) or (self._last_time is not None and now < self._last_time):
                raise ValueError("Limiter clock unavailable.")
            self._last_time = now
            for identity in list(self._entries):
                if self._entries[identity].expires <= now:
                    del self._entries[identity]
            pending = []
            for identity, limit in zip(identities, limits):
                policy = (limit.count, limit.seconds, limit.burst)
                old = self._entries.get(identity)
                if old is not None and old.policy != policy:
                    raise ValueError("Limiter policy changed during an active window.")
                accepted = deque(t for t in old.accepted if t > now - limit.seconds) if old else deque()
                tokens = min(limit.burst, old.tokens + (now - old.updated) * limit.count / limit.seconds) if old else limit.burst
                if len(accepted) >= limit.count or tokens < 1:
                    return False
                pending.append((identity, accepted, tokens, limit, policy))
            new_keys = sum(identity not in self._entries for identity in identities)
            if len(self._entries) + new_keys > max_keys:
                raise ValueError("Limiter capacity unavailable.")
            for identity, accepted, tokens, limit, policy in pending:
                accepted.append(now)
                self._entries[identity] = _Window(accepted, tokens - 1, now, now + limit.seconds, policy)
            return True


def admit_local_callback(phone=None, *, initial=False):
    """Independent opt-in layer; never bypasses persistent ingress checks."""
    try:
        enabled = current_app.config.get("USSD_RATE_LIMITING_ENABLED", False)
        if enabled is False:
            return
        if enabled is not True:
            raise CallbackRefused()
        limiter = current_app.extensions.get("ussd_local_limiter")
        if not isinstance(limiter, LocalUssdLimiter):
            raise CallbackRefused()
        # Validate every policy before any request is admitted.
        global_count = _positive("USSD_GLOBAL_CALLBACKS_PER_SECOND", 10000)
        global_burst = _positive("USSD_GLOBAL_CALLBACK_BURST", global_count)
        phone_count = _positive("USSD_PHONE_CALLBACKS_PER_MINUTE", 10000)
        phone_burst = _positive("USSD_PHONE_CALLBACK_BURST", phone_count)
        initial_count = _positive("USSD_NEW_SESSIONS_PER_MINUTE", phone_count)
        max_keys = _positive("USSD_RATE_LIMIT_MAX_KEYS", 20000)
        # A welcome request needs global, phone and initial entries.
        if max_keys < 3:
            raise CallbackRefused()
        if phone is None:
            limits = (CallbackLimit("global", b"ussd-global", global_count, 1, global_burst),)
        else:
            tag = limiter.phone_tag(phone)
            limits = (CallbackLimit("phone", tag, phone_count, 60, phone_burst),)
            if initial:
                limits += (CallbackLimit("initial", tag, initial_count, 60, initial_count),)
        accepted = limiter.admit(limits, max_keys=max_keys)
        if type(accepted) is not bool:
            raise CallbackRefused()
    except Exception:
        raise CallbackRefused() from None
    if not accepted:
        raise CallbackRefused(429)
