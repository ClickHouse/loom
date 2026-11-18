"""The codes of an erasure whose counts include a mirror."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# A stale unmarked writer cannot supersede a receipt that already counted mirrors.
MIRROR_VERSION_BIT = 1 << 63


def receipt_version(at_ns: int, *, mirror_marked: bool) -> int:
    """Marked receipts supersede every unmarked version; time orders each group."""
    return at_ns | (MIRROR_VERSION_BIT if mirror_marked else 0)


def codes_only(receipt: dict[str, Any]) -> dict[str, Any]:
    """Hide the whole receipt: subtracting a named namespace's counts reveals its mirror."""
    result = dict(receipt)
    for name, value in {
        "residue_count": 0,
        "residue": {},
        "census": None,
        "verified_at": None,
    }.items():
        if name in result:
            result[name] = value
    for name in ("steps", "steps_completed", "completed_steps", "previously_completed"):
        if name in result:
            result[name] = []
    for name in ("current_step", "steps_completed_count"):
        if name in result:
            result[name] = 0
    for name in ("failure_reason", "error", "last_error"):
        if result.get(name):
            result[name] = "ERASURE_FAILED"
    return result


def audit_subject(row: Mapping[str, Any], subject: dict[str, Any]) -> dict[str, Any]:
    """An audit row outlives the mirror and is readable by the namespace's admin."""
    return codes_only(subject) if row.get("mirror_marked") else subject
