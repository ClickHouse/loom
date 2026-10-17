"""Unit tests for as_of replay on the summary/observation legs (the fix that
makes point-in-time replay airtight across all retrieval legs, not just the
vector leg)."""

from __future__ import annotations

from datetime import datetime

import pytest

from server.memory.app.retrieval import fetch_entity_summaries, fetch_session_summaries
from server.memory.app.retrieval.summaries import _as_of_clause


def test_no_as_of_is_noop():
    params: dict = {}
    assert (
        _as_of_clause(
            None, "valid_time", valid_col="valid_at", created_col="created_at", params=params
        )
        is None
    )
    assert params == {}


def test_valid_time_coalesces_valid_then_created():
    params: dict = {}
    clause = _as_of_clause(
        datetime(2026, 9, 1),
        "valid_time",
        valid_col="valid_at",
        created_col="created_at",
        params=params,
    )
    assert "(valid_at IS NOT NULL AND valid_at <= {as_of:DateTime})" in clause
    # no anchor column on the summary tables: two-branch form
    assert "(valid_at IS NULL AND created_at <= {as_of:DateTime})" in clause
    assert "temporal_anchor" not in clause
    assert params["as_of"] == datetime(2026, 9, 1)


def test_knowledge_time_uses_created_only():
    params: dict = {}
    clause = _as_of_clause(
        datetime(2026, 9, 1),
        "knowledge_time",
        valid_col="valid_from",
        created_col="created_at",
        params=params,
    )
    assert clause == "(created_at <= {as_of:DateTime})"
    assert "valid_from" not in clause  # knowledge-time ignores validity
    assert params["as_of"] == datetime(2026, 9, 1)


def test_column_names_are_respected():
    # entity_summaries uses valid_from, not valid_at.
    clause = _as_of_clause(
        datetime(2026, 9, 1),
        "valid_time",
        valid_col="valid_from",
        created_col="created_at",
        params={},
    )
    assert "valid_from IS NOT NULL" in clause


# --- Regression: SELECT alias must not shadow the as_of WHERE column ----------
# The as_of clause references the base column (`valid_at` / `valid_from`). If the
# SELECT aliases a `toString(coalesce(col, ...))` expression to that SAME name,
# the analyzer binds the WHERE comparison to the String alias, raising "no
# supertype for String, DateTime" — which the leg's `except: return []` swallows,
# silently killing the leg under ANY as_of. These tests run the real legs with a
# stub CH and assert the generated SQL keeps the alias distinct from the column.


class _CaptureCH:
    def __init__(self) -> None:
        self.sql = ""

    async def query_all(self, sql: str, params=None):
        self.sql = sql
        return []


@pytest.mark.asyncio
async def test_entity_summary_alias_does_not_shadow_asof_column():
    ch = _CaptureCH()
    await fetch_entity_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1] * 4,
        embedding_model="m",
        entity_filter=["x"],
        as_of=datetime(2026, 9, 1),
    )
    # The as_of comparison must be present on the bare DateTime column...
    assert "valid_from <= {as_of:DateTime}" in ch.sql
    # ...and the String projection must be aliased to a DIFFERENT name.
    assert "AS valid_from_iso" in ch.sql
    assert "AS valid_from\n" not in ch.sql and "AS valid_from " not in ch.sql


@pytest.mark.asyncio
async def test_session_summary_alias_does_not_shadow_asof_column():
    ch = _CaptureCH()
    await fetch_session_summaries(
        ch,
        org="o",
        namespace="ns",
        agent="bot",
        user_id="u",
        query_vec=[0.1] * 4,
        embedding_model="m",
        as_of=datetime(2026, 9, 1),
    )
    assert "valid_at <= {as_of:DateTime}" in ch.sql
    assert "AS valid_at_iso" in ch.sql
    assert "AS valid_at\n" not in ch.sql and "AS valid_at " not in ch.sql
