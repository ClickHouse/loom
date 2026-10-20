"""Checks for the numbers a client or a shared document hands the server."""

from __future__ import annotations

import math
from typing import Any


def finite_non_negative(value: Any) -> bool:
    """An int or float (not a bool) that is finite and >= 0; an int too large for
    a float is not finite."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def parse_budget_value(raw: Any) -> int | float | None:
    """A latency budget override entry: int/float milliseconds, or "<n>x" multiplier
    returned as a float factor. None when unusable (negative, not finite)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return int(raw) if finite_non_negative(raw) else None
    if isinstance(raw, str):
        text = raw.strip().lower()
        try:
            number = float(text.removesuffix("x"))
        except ValueError:
            return None
        if not finite_non_negative(number):
            return None
        return number if text.endswith("x") else int(number)
    return None
