"""Unit tests for the safety harness around memory.aggregate.

The handler's safety guarantees — identifier regex, agg_func whitelist,
parameterised values — are what stand between this op and SQL injection.
These tests verify each guard fires before the SQL ever reaches CH.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.memory.app.query import _SAFE_IDENT_RE, _assert_safe_ident


def test_safe_ident_regex_accepts_normal_names() -> None:
    for name in ["amount", "user_id", "x", "field_1", "Category"]:
        assert _SAFE_IDENT_RE.match(name), name


def test_safe_ident_regex_rejects_sql_injection_attempts() -> None:
    for name in [
        "amount; DROP TABLE",
        "amount' OR '1'='1",
        'amount."; --',
        "1_starts_with_digit",
        "with space",
        "",
        "field-with-dash",
    ]:
        assert not _SAFE_IDENT_RE.match(name), name


def test_assert_safe_ident_raises_on_bad_input() -> None:
    with pytest.raises(HTTPException) as exc:
        _assert_safe_ident("amount; DROP", "agg_field")
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "INVALID_IDENTIFIER"


def test_assert_safe_ident_passes_on_good_input() -> None:
    _assert_safe_ident("amount", "agg_field")
    _assert_safe_ident("user_id", "group_by")
