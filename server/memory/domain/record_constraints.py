"""Field constraints on typed records.

A schema field says its type, whether it is required, whether it is part
of the record's identity, and (for strings) an enumeration. Constraints add
"this string matches ^PR-\\d+$", "this number is between 0 and 1" and
"this string is at most 64 characters": carried on the field and enforced
at write, instead of living in the extractor's prompt or nowhere.

Constraints are stored as one JSON object per field
(`memory_schema_fields.constraints`) with the keys below.
Everything here is pure; the handler calls `validate_shape` at register
and `violation` at write.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Keys a constraints object may carry, by field type.
STRING_KEYS = ("pattern", "min_length", "max_length")
NUMBER_KEYS = ("min_value", "max_value")
KEYS = STRING_KEYS + NUMBER_KEYS

_MAX_PATTERN_LEN = 256


def from_field(field: Any) -> dict[str, Any]:
    """The non-empty constraints a SchemaFieldInput declares, as the JSON
    object that is stored. Empty dict when the field declares none."""
    out: dict[str, Any] = {}
    pattern = str(getattr(field, "pattern", "") or "")
    if pattern:
        out["pattern"] = pattern
    for key in ("min_length", "max_length", "min_value", "max_value"):
        value = getattr(field, key, None)
        if value is not None:
            out[key] = value
    return out


def validate_shape(field_name: str, field_type: str, constraints: dict[str, Any]) -> str | None:
    """A reason the declaration is invalid, or None. Type-checks each key
    against the field type, compiles the pattern, and orders the bounds."""
    if not constraints:
        return None
    unknown = sorted(set(constraints) - set(KEYS))
    if unknown:
        return f"field {field_name!r}: unknown constraint(s) {unknown}"
    is_string = field_type == "string"
    is_number = field_type in {"number", "integer"}
    for key in STRING_KEYS:
        if key in constraints and not is_string:
            return f"field {field_name!r}: {key} applies to string fields only"
    for key in NUMBER_KEYS:
        if key in constraints and not is_number:
            return f"field {field_name!r}: {key} applies to number/integer fields only"
    pattern = constraints.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            return f"field {field_name!r}: pattern must be a non-empty string"
        if len(pattern) > _MAX_PATTERN_LEN:
            return f"field {field_name!r}: pattern longer than {_MAX_PATTERN_LEN} chars"
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"field {field_name!r}: pattern does not compile ({exc})"
    for key in ("min_length", "max_length"):
        v = constraints.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            return f"field {field_name!r}: {key} must be a non-negative integer"
    lo, hi = constraints.get("min_length"), constraints.get("max_length")
    if lo is not None and hi is not None and lo > hi:
        return f"field {field_name!r}: min_length {lo} exceeds max_length {hi}"
    for key in NUMBER_KEYS:
        v = constraints.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int | float)):
            return f"field {field_name!r}: {key} must be a number"
    lo, hi = constraints.get("min_value"), constraints.get("max_value")
    if lo is not None and hi is not None and lo > hi:
        return f"field {field_name!r}: min_value {lo} exceeds max_value {hi}"
    return None


def violation(
    field_name: str,
    field_type: str,
    constraints: dict[str, Any],
    *,
    value_string: str,
    value_number: float,
) -> str | None:
    """Why a concrete value breaks the field's constraints, or None.
    `value_string` is the coerced string surface, `value_number` the coerced
    numeric one — the same columns the row stores, so the check and the
    stored value can never disagree."""
    if not constraints:
        return None
    if field_type == "string":
        pattern = constraints.get("pattern")
        if pattern and re.search(str(pattern), value_string) is None:
            return f"field {field_name!r}: {value_string!r} does not match pattern {pattern!r}"
        lo, hi = constraints.get("min_length"), constraints.get("max_length")
        n = len(value_string)
        if lo is not None and n < int(lo):
            return f"field {field_name!r}: length {n} below min_length {lo}"
        if hi is not None and n > int(hi):
            return f"field {field_name!r}: length {n} above max_length {hi}"
    elif field_type in {"number", "integer"}:
        lo, hi = constraints.get("min_value"), constraints.get("max_value")
        if lo is not None and value_number < float(lo):
            return f"field {field_name!r}: {value_number} below min_value {lo}"
        if hi is not None and value_number > float(hi):
            return f"field {field_name!r}: {value_number} above max_value {hi}"
    return None


def dumps(constraints: dict[str, Any]) -> str:
    return json.dumps(constraints, sort_keys=True, separators=(",", ":")) if constraints else "{}"


def loads(raw: Any) -> dict[str, Any]:
    """Tolerant read of the stored column: '' / '{}' / malformed → {}."""
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text or text == "{}":
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}
