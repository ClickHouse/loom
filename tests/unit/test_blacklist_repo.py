"""Unit tests for server/memory/store/blacklist.py."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
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


class _TombstoneCH(StubCH):
    """Supplies database rows through the real bounded JSON reader."""

    def __init__(self, rows: list[dict[str, Any]], *, partition_key: str = "") -> None:
        super().__init__()
        self.stream_calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = [
            [
                {
                    "engine": "ReplacingMergeTree",
                    "engine_full": "ReplacingMergeTree(created_at)",
                    "sorting_key": "org, namespace, pattern_id",
                }
            ],
            [
                {"name": name, "type": kind}
                for name, kind in {
                    "pattern_id": "UUID",
                    "org": "LowCardinality(String)",
                    "namespace": "LowCardinality(String)",
                    "pattern": "String",
                    "pattern_type": "Enum8('substring'=1,'regex'=2)",
                    "scope": "Enum8('namespace'=1,'org'=2,'global'=3)",
                    "reason": "String",
                    "created_by": "String",
                    "hit_count": "UInt64",
                    "created_at": "DateTime",
                    "tombstoned_at": "Nullable(DateTime)",
                    "last_hit_at": "Nullable(DateTime)",
                }.items()
            ],
            rows,
            [{"partition_key": partition_key, "now": 1_800_000_000}],
        ]

    @asynccontextmanager
    async def raw_stream(self, sql, params=None, settings=None):
        self.stream_calls.append((sql, params or {}))
        rows = self.responses.pop(0)

        async def chunks():
            for row in rows:
                yield (json.dumps(row) + "\n").encode()

        yield chunks()


def _tombstone_row(created_at: int = 1_700_000_000) -> dict[str, Any]:
    return {
        "pattern_id": "a046002d-c888-4b54-852b-167f1cb8a443",
        "org": "acme",
        "namespace": "clinical",
        "pattern": "medical",
        "pattern_type": "substring",
        "scope": "namespace",
        "reason": "subject restriction",
        "created_by": "admin",
        "hit_count": 19,
        "created_at": created_at,
        "tombstoned_at": None,
        "last_hit_at": 1_650_000_000,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("created_at", [1_700_000_000, 1_900_000_000])
@pytest.mark.parametrize("selected", [None, ("acme", "clinical", "namespace")])
async def test_tombstone_preserves_selected_row_with_newer_version(created_at, selected) -> None:
    row = _tombstone_row(created_at)
    ch = _TombstoneCH([row])
    when = datetime(2026, 5, 17, tzinfo=UTC)
    assert await tombstone_pattern(row["pattern_id"], when, ch=ch, selected=selected)
    table, rows, settings = ch.insert_calls[0]
    assert table == "loom.blacklist" and len(rows) == 1
    expected = {
        **row,
        "created_at": datetime.fromtimestamp(max(1_800_000_000, created_at + 1), UTC).replace(
            tzinfo=None
        ),
        "tombstoned_at": when.replace(tzinfo=None),
        "last_hit_at": datetime.fromtimestamp(row["last_hit_at"], UTC).replace(tzinfo=None),
    }
    assert rows[0] == expected
    assert settings["async_insert"] == 0 and settings["wait_for_async_insert"] == 1
    assert ch.execute_calls == []
    sql, params = ch.stream_calls[2]
    assert "FINAL" in sql and params["pid"] == row["pattern_id"]
    if selected is not None:
        assert "org = {org:String}" in sql and params["org"] == selected[0]
        assert "namespace = {ns:String}" in sql and params["ns"] == selected[1]
        assert "scope = {scope:String}" in sql and params["scope"] == selected[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("already_removed", [False, True])
async def test_tombstone_missing_or_already_removed_does_not_write(already_removed) -> None:
    row = _tombstone_row()
    row["tombstoned_at"] = 1_750_000_000
    ch = _TombstoneCH([row] if already_removed else [])
    assert not await tombstone_pattern(row["pattern_id"], datetime(2026, 5, 17, tzinfo=UTC), ch=ch)
    assert ch.insert_calls == [] and ch.execute_calls == []


@pytest.mark.asyncio
async def test_tombstone_unrepresentable_version_does_not_write() -> None:
    row = _tombstone_row(2**32 - 1)
    ch = _TombstoneCH([row])
    with pytest.raises(ValueError, match="successor version"):
        await tombstone_pattern(row["pattern_id"], datetime(2026, 5, 17, tzinfo=UTC), ch=ch)
    assert ch.insert_calls == [] and ch.execute_calls == []


@pytest.mark.asyncio
async def test_tombstone_partitioned_storage_does_not_write() -> None:
    row = _tombstone_row()
    ch = _TombstoneCH([row], partition_key="toYYYYMM(created_at)")
    with pytest.raises(ValueError, match="replacement storage"):
        await tombstone_pattern(row["pattern_id"], datetime(2026, 5, 17, tzinfo=UTC), ch=ch)
    assert ch.insert_calls == [] and ch.execute_calls == []


@pytest.mark.asyncio
async def test_tombstone_ambiguous_uuid_does_not_write() -> None:
    row = _tombstone_row()
    ch = _TombstoneCH([row, {**row, "org": "another"}])
    with pytest.raises(ValueError, match="ambiguous"):
        await tombstone_pattern(row["pattern_id"], datetime(2026, 5, 17, tzinfo=UTC), ch=ch)
    assert ch.insert_calls == [] and ch.execute_calls == []
