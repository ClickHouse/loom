"""Unit tests for server/memory/store/blacklist.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.store.blacklist import (
    find_active_pattern,
    insert_row,
    list_for_scope,
    tombstone_pattern,
)


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


@pytest.mark.asyncio
async def test_insert_row_forces_synchronous_visibility() -> None:
    ch = StubCH()
    await insert_row({"pattern_id": "x"}, ch=ch)
    table, _, settings = ch.insert_calls[0]
    assert table == "loom.blacklist"
    assert settings.get("async_insert") == 0


@pytest.mark.asyncio
async def test_list_for_scope_none_returns_all_visible_scopes() -> None:
    """When scope=None, the predicate ORs the three scope shapes so
    a list-without-filter returns namespace + org-wide + global rows."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await list_for_scope("clickhouse", "ns", ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "namespace = '*'" in sql
    assert "org = '*'" in sql
    assert "tombstoned_at IS NULL" in sql


@pytest.mark.asyncio
async def test_list_for_scope_global_filters_to_scope_field() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await list_for_scope("clickhouse", "ns", scope="global", ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "scope = 'global'" in sql


@pytest.mark.asyncio
async def test_find_active_pattern_falls_back_to_global() -> None:
    """The blacklist.remove path tries org-scoped lookup first, then
    a global one. Each call here is org-or-not-supplied."""
    ch = StubCH()
    # With org filter
    ch.query_one_returns.append(None)
    await find_active_pattern("pid-1", org="clickhouse", ch=ch)
    sql, params = ch.query_one_calls[0]
    assert "org = {org:String}" in sql
    assert params["org"] == "clickhouse"

    # Without org filter (global fallback)
    ch.query_one_returns.append({"pattern_id": "pid-1"})
    out = await find_active_pattern("pid-1", ch=ch)
    sql2, params2 = ch.query_one_calls[1]
    assert "org = " not in sql2
    assert "org" not in params2
    assert out == {"pattern_id": "pid-1"}


@pytest.mark.asyncio
async def test_tombstone_pattern_uses_mutations_sync() -> None:
    """mutations_sync=2 so the next list call doesn't see the row."""
    ch = StubCH()
    await tombstone_pattern("pid-1", datetime(2026, 5, 17, tzinfo=UTC), ch=ch)
    sql, params = ch.execute_calls[0]
    assert "ALTER TABLE loom.blacklist" in sql
    assert "tombstoned_at = {ts:DateTime}" in sql
    assert "mutations_sync=2" in sql
    assert params["pid"] == "pid-1"
