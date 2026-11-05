"""Offline-capable PIN cryptography only; no database, routes, or attempt policy."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from functools import wraps

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from app.services.ussd_hash_capacity import scrypt_slot


HASH_METHOD = "scrypt:131072:8:1"
SALT_LENGTH = 32
_HASH_PATTERN = re.compile(r"scrypt:131072:8:1\$[A-Za-z0-9]{32}\$[0-9a-f]{128}")
_CONFIG_MESSAGE = "USSD PIN verification is unavailable."


class UssdPinError(Exception):
    """An internal PIN operation failed; messages contain no diagnostic values."""


class UssdPinConfigurationError(UssdPinError):
    """Required key configuration is missing or invalid, not a wrong PIN."""


class UssdPinValidationError(UssdPinError, ValueError):
    """Invalid enrollment input with a fixed, non-sensitive message."""


class UssdPinCredentialError(UssdPinError):
    """Stored credential integrity failure, not an incorrect PIN attempt."""


def _safe_operation(method):
    @wraps(method)
    def guarded(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except UssdPinError:
            raise
        except Exception:
            # Do not expose hashing-library diagnostics, inputs, or key material.
            raise UssdPinError("USSD PIN operation could not be completed.") from None
    return guarded


def _valid_pin(pin):
    return isinstance(pin, str) and re.fullmatch(r"[0-9]{4}", pin) is not None


def _valid_farmer_id(farmer_id):
    return type(farmer_id) is int and 0 < farmer_id <= 18446744073709551615


def _valid_version(version):
    return type(version) is int and 1 <= version <= 65535


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UssdPinConfigurationError(_CONFIG_MESSAGE)
        result[key] = value
    return result


def _secret_forms(value):
    """Compare both raw and encoded forms to catch accidental key reuse."""
    if isinstance(value, bytes):
        forms = [value]
    elif isinstance(value, str):
        forms = [value.encode("utf-8")]
    elif value is None:
        return []
    else:
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)
    try:
        forms.append(base64.b64decode(forms[0], validate=True))
    except (ValueError, binascii.Error):
        pass
    return forms


def _load_keyring():
    config = current_app.config
    raw = config.get("USSD_PIN_PEPPERS")
    active = config.get("USSD_PIN_CURRENT_KEY_VERSION")
    if not isinstance(raw, str) or not 1 <= len(raw) <= 8192:
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)
    if isinstance(active, str) and re.fullmatch(r"[1-9][0-9]{0,4}", active):
        active = int(active)
    if not _valid_version(active):
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError):
        raise UssdPinConfigurationError(_CONFIG_MESSAGE) from None
    if not isinstance(parsed, dict) or not 1 <= len(parsed) <= 32:
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)
    forbidden = (_secret_forms(config.get("SECRET_KEY"))
                 + _secret_forms(config.get("USSD_METADATA_HMAC_KEY")))
    keys = {}
    for version, encoded in parsed.items():
        if (not isinstance(version, str) or not re.fullmatch(r"[1-9][0-9]{0,4}", version)
                or not _valid_version(int(version)) or not isinstance(encoded, str)
                or len(encoded) != 44):
            raise UssdPinConfigurationError(_CONFIG_MESSAGE)
        try:
            key = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise UssdPinConfigurationError(_CONFIG_MESSAGE) from None
        if (len(key) != 32 or base64.b64encode(key).decode("ascii") != encoded
                or any(hmac.compare_digest(key, other) for other in forbidden + list(keys.values()))):
            raise UssdPinConfigurationError(_CONFIG_MESSAGE)
        keys[int(version)] = key
    if active not in keys:
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)
    return keys, active


def _peppered_pin(key, farmer_id, version, pin):
    # Fixed-width identity/version plus exact ASCII PIN: no ambiguous framing.
    message = (b"farmadvis/ussd/pin/v1\x00" + farmer_id.to_bytes(8, "big")
               + version.to_bytes(2, "big") + pin.encode("ascii"))
    return hmac.digest(key, message, hashlib.sha256).hex()


@_safe_operation
def validate_ussd_pin_credential(pin_hash, pin_key_version):
    """Validate storage/key availability without hashing or counting attempts."""
    if (not isinstance(pin_hash, str) or len(pin_hash) > 255
            or not _HASH_PATTERN.fullmatch(pin_hash) or not _valid_version(pin_key_version)):
        raise UssdPinCredentialError("USSD PIN verification is unavailable.")
    keys, _ = _load_keyring()
    if pin_key_version not in keys:
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)


@_safe_operation
def hash_ussd_pin(farmer_id, pin):
    """Return only the hash and pepper version for future repository storage."""
    if not _valid_pin(pin):
        raise UssdPinValidationError("Use exactly four ASCII digits for the USSD PIN.")
    if not _valid_farmer_id(farmer_id):
        raise UssdPinValidationError("A valid farmer ID is required.")
    keys, version = _load_keyring()
    with scrypt_slot():
        encoded = generate_password_hash(
            _peppered_pin(keys[version], farmer_id, version, pin),
            method=HASH_METHOD, salt_length=SALT_LENGTH,
        )
    if not isinstance(encoded, str) or not _HASH_PATTERN.fullmatch(encoded):
        raise UssdPinError("USSD PIN operation could not be completed.")
    return {"pin_hash": encoded, "pin_key_version": version}


@_safe_operation
def verify_ussd_pin(farmer_id, pin, pin_hash, pin_key_version):
    """Return bool for credential mismatch; configuration/outage errors raise.

    Use the stored pepper version, never fall back to the current version or try
    every key. This function neither authenticates an account nor counts attempts.
    """
    if (not _valid_farmer_id(farmer_id) or not _valid_pin(pin)
            or not _valid_version(pin_key_version) or not isinstance(pin_hash, str)
            or len(pin_hash) > 255 or not _HASH_PATTERN.fullmatch(pin_hash)):
        return False
    keys, _ = _load_keyring()
    if pin_key_version not in keys:
        raise UssdPinConfigurationError(_CONFIG_MESSAGE)
    with scrypt_slot():
        return check_password_hash(
            pin_hash, _peppered_pin(keys[pin_key_version], farmer_id, pin_key_version, pin),
        )
