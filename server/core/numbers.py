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
