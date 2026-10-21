"""A record field's value against its schema: the field-name rule, coercion to
the field's type, and the constraints a value must meet."""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException

from server.memory.domain import record_constraints

FIELD_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_TRUE_TOKENS = {"true", "yes", "1", "t", "y"}
_FALSE_TOKENS = {"false", "no", "0", "f", "n"}


def _bad_value(field_name: str, message: str) -> None:
    raise HTTPException(
        status_code=400,
        detail={
            "code": "BAD_FIELD_VALUE",
            "message": f"field {field_name!r}: {message}",
            "field": field_name,
        },
    )


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value in (0, 1):
            return bool(value)
        _bad_value(field_name, f"expected boolean, got number {value!r}")
    s = str(value).strip().lower()
    if s in _TRUE_TOKENS:
        return True
    if s in _FALSE_TOKENS:
        return False
    _bad_value(field_name, f"expected boolean, got {value!r}")
    return False  # unreachable: _bad_value raises


def _coerce_number(value: Any, field_name: str, *, integer: bool) -> float:
    if isinstance(value, bool):
        _bad_value(field_name, "expected number, got boolean")
    if isinstance(value, (int, float)):
        n = float(value)
    else:
        try:
            n = float(str(value).strip())
        except (TypeError, ValueError):
            _bad_value(
                field_name,
                f"expected {'integer' if integer else 'number'}, got {value!r}",
            )
            return 0.0  # unreachable
    if integer and n != int(n):
        _bad_value(field_name, f"expected integer, got {value!r}")
    return n


def coerce_record_value(
    value: Any,
    field_type: str,
    *,
    field_name: str,
    enum_values: list[str] | None = None,
    state: str = "value",
    constraints: dict[str, Any] | None = None,
) -> tuple[str, float, bool]:
    """Validate + coerce one field into the three physical columns.

    Non-'value' states (unknown/absent/rejected) carry no concrete value, so
    they store type-neutral zeros — an explicitly-unknown field is never
    recorded as a real 0 / '' / false. Constraints are checked on the coerced surfaces,
    after type and enum, so the value the check saw is the value the row stores.
    """
    if state != "value":
        return ("", 0.0, False)
    if field_type == "boolean":
        b = _coerce_bool(value, field_name)
        return ("true" if b else "false", 1.0 if b else 0.0, b)
    if field_type in {"number", "integer"}:
        n = _coerce_number(value, field_name, integer=(field_type == "integer"))
        out_n = (str(int(n)) if field_type == "integer" else str(n), n, bool(n))
        _check_constraints(field_name, field_type, constraints, out_n[0], n)
        return out_n
    s = "" if value is None else str(value)
    if enum_values and s not in set(enum_values):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ENUM_VIOLATION",
                "message": f"field {field_name!r}: {s!r} not in {list(enum_values)}",
                "field": field_name,
            },
        )
    _check_constraints(field_name, field_type, constraints, s, 0.0)
    return (s, 0.0, False)


def _check_constraints(
    field_name: str,
    field_type: str,
    constraints: dict[str, Any] | None,
    value_string: str,
    value_number: float,
) -> None:
    if not constraints:
        return
    reason = record_constraints.violation(
        field_name,
        field_type,
        constraints,
        value_string=value_string,
        value_number=value_number,
    )
    if reason:
        raise HTTPException(
            status_code=400,
            detail={"code": "CONSTRAINT_VIOLATION", "message": reason, "field": field_name},
        )


def _check_record_fields(
    input: Any,
    field_defs: list[dict[str, Any]],
    field_map: dict[str, dict[str, Any]],
) -> None:
    """400 on a field the schema does not declare or a required field left out."""
    # Schema is enforced at write: reject undeclared fields; require declared-
    # required fields to be present (any state — an explicit 'unknown' counts).
    unknown_fields = [f for f in input.fields if f not in field_map]
    if unknown_fields:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "UNKNOWN_FIELD",
                "message": (f"fields not in schema {input.schema_name!r}: {unknown_fields}"),
                "fields": unknown_fields,
            },
        )
    missing_required = [
        str(f["field_name"])
        for f in field_defs
        if bool(f.get("required")) and str(f["field_name"]) not in input.fields
    ]
    if missing_required:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_REQUIRED_FIELD",
                "message": (
                    f"required fields missing for {input.schema_name!r}: {missing_required}"
                ),
                "fields": missing_required,
            },
        )
