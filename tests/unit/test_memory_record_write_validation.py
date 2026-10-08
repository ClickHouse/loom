"""Unit tests for the write-side validation gate on memory.record.write.

Covers the type/enum coercion that RAISES on a mismatch instead of a
`float(value or 0)` silent zero, and the explicit-unknown rule that a
non-'value' state never records a concrete value. Pure functions — no CH.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.memory.domain.record_values import _coerce_bool, _coerce_number, _coerce_record_value


def test_non_value_states_store_no_concrete_value() -> None:
    # An explicitly-unknown/absent/rejected field must not be recorded as a
    # real 0 / '' / false that a negative-query could not distinguish.
    for state in ("unknown", "absent", "rejected"):
        assert _coerce_record_value(123, "number", field_name="x", state=state) == ("", 0.0, False)


def test_boolean_accepts_truthy_tokens() -> None:
    assert _coerce_bool(True, "f") is True
    assert _coerce_bool("yes", "f") is True
    assert _coerce_bool("FALSE", "f") is False
    assert _coerce_bool(1, "f") is True
    assert _coerce_bool(0, "f") is False


def test_boolean_rejects_garbage() -> None:
    with pytest.raises(HTTPException):
        _coerce_bool("maybe", "f")
    with pytest.raises(HTTPException):
        _coerce_bool(7, "f")


def test_integer_rejects_bool_and_fractional() -> None:
    assert _coerce_number(5, "f", integer=True) == 5.0
    assert _coerce_number("5", "f", integer=True) == 5.0
    with pytest.raises(HTTPException):
        _coerce_number(True, "f", integer=True)  # bool is not a number
    with pytest.raises(HTTPException):
        _coerce_number(5.5, "f", integer=True)  # not integral
    with pytest.raises(HTTPException):
        _coerce_number("nope", "f", integer=True)


def test_number_accepts_numeric_strings_rejects_bool() -> None:
    assert _coerce_number("3.5", "f", integer=False) == 3.5
    with pytest.raises(HTTPException):
        _coerce_number(False, "f", integer=False)


def test_record_value_coerces_each_type() -> None:
    assert _coerce_record_value(True, "boolean", field_name="b") == ("true", 1.0, True)
    assert _coerce_record_value("12", "integer", field_name="n") == ("12", 12.0, True)
    assert _coerce_record_value("1.5", "number", field_name="n") == ("1.5", 1.5, True)
    assert _coerce_record_value("hi", "string", field_name="s") == ("hi", 0.0, False)


def test_enum_violation_raises() -> None:
    assert _coerce_record_value(
        "coffee", "string", field_name="cat", enum_values=["coffee", "lunch"]
    ) == ("coffee", 0.0, False)
    with pytest.raises(HTTPException):
        _coerce_record_value("tea", "string", field_name="cat", enum_values=["coffee", "lunch"])
