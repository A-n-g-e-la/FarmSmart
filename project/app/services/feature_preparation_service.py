"""Shared validation and area units for fertilizer calculations."""

import math
from numbers import Real


ACRE_TO_HA = 0.40468564224


def number(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (Real, str)):
        raise ValueError(f"{name} must be numeric")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise ValueError(f"{name} is outside its finite allowed range")
    return result


def object_input(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
