"""The additive pool arms (anchor pool, key match, digest descent) apply the fact leg's
read-scope and as-of predicates, so a `read_scope=user|session` read or an as-of replay
cannot be served another user's, another session's, or a later row through them."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.sql import (
    _anchor_pool_where,
    _digest_descent_where,
    _key_match_where,
    key_match_component_sql,
)

_ARMS = [(_anchor_pool_where, "e."), (_key_match_where, "e."), (_digest_descent_where, "")]


def _input(**extra: Any) -> MemorySearchInput:
    return MemorySearchInput(org="o", namespace="n", agent="a", query="q", **extra)


@pytest.mark.parametrize(("arm", "col"), _ARMS)
def test_each_arm_carries_the_read_scope(arm: Any, col: str) -> None:
    params: dict[str, Any] = {}
    where = " AND ".join(
        arm(
            _input(
                user_id="u1",
                session_id="s1",
                session_ids=["s1", "s2"],
            ),
            params,
        )
    )
    assert f"{col}user_id = {{rs_user_id:String}}" in where
    assert f"{col}session_id = {{rs_session_id:String}}" in where
    assert f"{col}session_id IN {{rs_session_ids:Array(String)}}" in where
    assert params["rs_user_id"] == "u1" and params["rs_session_ids"] == ["s1", "s2"]


@pytest.mark.parametrize(("arm", "col"), _ARMS)
def test_a_namespace_read_adds_nothing(arm: Any, col: str) -> None:
    params: dict[str, Any] = {}
    where = " AND ".join(arm(_input(), params))
    assert "rs_" not in where and not any(k.startswith("rs_") for k in params)


def test_the_component_statement_selects_the_columns_the_predicates_read() -> None:
    sql = key_match_component_sql("TRUE")
    for column in ("user_id", "session_id", "created_at", "valid_at", "invalid_at"):
        assert column in sql
