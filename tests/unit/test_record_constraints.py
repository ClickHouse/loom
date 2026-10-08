"""A schema field carries its constraints.

CH-free: the pure service, the register-time shape check through the
handler's validator, and write-time enforcement through the coercer that
produces the stored columns.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.memory.api.models_records import SchemaFieldInput
from server.memory.app.records import _validate_schema_fields
from server.memory.domain import record_constraints as rc
from server.memory.domain.record_values import _coerce_record_value

# ----------------------------------------------------------------------
# Declaration
# ----------------------------------------------------------------------


def test_from_field_keeps_only_declared_constraints() -> None:
    assert rc.from_field(SchemaFieldInput(name="a")) == {}
    f = SchemaFieldInput(name="pr", pattern=r"^PR-\d+$", max_length=12)
    assert rc.from_field(f) == {"pattern": r"^PR-\d+$", "max_length": 12}
    assert rc.loads(rc.dumps(rc.from_field(f))) == rc.from_field(f)
    assert rc.dumps({}) == "{}" and rc.loads("") == {} and rc.loads("not json") == {}


@pytest.mark.parametrize(
    ("field_type", "constraints", "fragment"),
    [
        ("number", {"pattern": "x"}, "string fields only"),
        ("string", {"min_value": 1}, "number/integer fields only"),
        ("string", {"pattern": "("}, "does not compile"),
        ("string", {"pattern": ""}, "non-empty"),
        ("string", {"min_length": 5, "max_length": 2}, "exceeds max_length"),
        ("integer", {"min_value": 10, "max_value": 1}, "exceeds min_value".replace("min", "max")),
        ("string", {"bogus": 1}, "unknown constraint"),
        ("string", {"min_length": -1}, "non-negative"),
    ],
)
def test_validate_shape_rejects_bad_declarations(
    field_type: str, constraints: dict, fragment: str
) -> None:
    reason = rc.validate_shape("f", field_type, constraints)
    assert reason and fragment in reason, reason


def test_validate_shape_accepts_well_formed_declarations() -> None:
    assert (
        rc.validate_shape("f", "string", {"pattern": r"^[a-z]+$", "min_length": 1, "max_length": 8})
        is None
    )
    assert rc.validate_shape("f", "number", {"min_value": 0, "max_value": 1.0}) is None
    assert rc.validate_shape("f", "integer", {"min_value": -5}) is None
    assert rc.validate_shape("f", "boolean", {}) is None


def test_register_validator_surfaces_bad_constraint_as_400() -> None:
    with pytest.raises(HTTPException) as exc:
        _validate_schema_fields([SchemaFieldInput(name="score", type="number", pattern="x")])
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "BAD_CONSTRAINT" and exc.value.detail["field"] == "score"
    # A clean declaration passes the same gate.
    _validate_schema_fields(
        [SchemaFieldInput(name="score", type="number", min_value=0, max_value=1)]
    )


# ----------------------------------------------------------------------
# Enforcement at write, on the coerced surfaces
# ----------------------------------------------------------------------


def test_violation_checks_the_right_surface_per_type() -> None:
    c = {"pattern": r"^PR-\d+$", "max_length": 8}
    assert rc.violation("pr", "string", c, value_string="PR-42", value_number=0.0) is None
    assert "does not match" in (
        rc.violation("pr", "string", c, value_string="issue-42", value_number=0.0) or ""
    )
    assert "above max_length" in (
        rc.violation("pr", "string", c, value_string="PR-123456789", value_number=0.0) or ""
    )
    n = {"min_value": 0, "max_value": 1}
    assert rc.violation("p", "number", n, value_string="0.5", value_number=0.5) is None
    assert "above max_value" in (
        rc.violation("p", "number", n, value_string="1.5", value_number=1.5) or ""
    )
    assert "below min_value" in (
        rc.violation("p", "integer", n, value_string="-1", value_number=-1) or ""
    )
    # Booleans carry no constraints; nothing to violate.
    assert (
        rc.violation("b", "boolean", {"min_value": 1}, value_string="true", value_number=1.0)
        is None
    )


def test_coerce_enforces_constraints_after_type_and_enum() -> None:
    ok = _coerce_record_value(
        "PR-7", "string", field_name="pr", constraints={"pattern": r"^PR-\d+$"}
    )
    assert ok == ("PR-7", 0.0, False)
    with pytest.raises(HTTPException) as exc:
        _coerce_record_value("7", "string", field_name="pr", constraints={"pattern": r"^PR-\d+$"})
    assert exc.value.detail["code"] == "CONSTRAINT_VIOLATION" and exc.value.detail["field"] == "pr"
    # Numbers are range-checked on the coerced number, so "0.75" as text works.
    assert (
        _coerce_record_value("0.75", "number", field_name="p", constraints={"max_value": 1})[1]
        == 0.75
    )
    with pytest.raises(HTTPException) as exc:
        _coerce_record_value("3", "integer", field_name="p", constraints={"max_value": 1})
    assert exc.value.detail["code"] == "CONSTRAINT_VIOLATION"
    # Enum still wins first with its own code.
    with pytest.raises(HTTPException) as exc:
        _coerce_record_value(
            "z", "string", field_name="e", enum_values=["a"], constraints={"max_length": 1}
        )
    assert exc.value.detail["code"] == "ENUM_VIOLATION"


def test_non_value_states_skip_constraints() -> None:
    """An explicitly unknown field stores no concrete value and cannot fail a
    rule about the value it does not have."""
    assert _coerce_record_value(
        None,
        "string",
        field_name="pr",
        state="unknown",
        constraints={"pattern": "^x$", "min_length": 3},
    ) == ("", 0.0, False)


def test_field_constraints_pass_through_the_schema_field() -> None:
    f = {"name": "pr", "type": "string", "pattern": r"^PR-\d+$", "max_length": 12}
    assert SchemaFieldInput(**f).pattern == r"^PR-\d+$"
    assert rc.from_field(SchemaFieldInput(**f)) == {"pattern": r"^PR-\d+$", "max_length": 12}
