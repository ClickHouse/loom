"""Unit tests for server/memory/store/conflicts.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.store.conflicts import (
    allowlist_exists,
    allowlist_insert,
    allowlist_list,
    allowlist_remove,
    list_conflicted,
    lookup_keys_for_ids,
    lookup_target,
    mark_active_by_ids,
    tombstone_ids,
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
async def test_list_conflicted_filters_and_paginates() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await list_conflicted(
        "clickhouse", "ns", before_version=1000, limit=50, ch=ch, scope=WHOLE_NAMESPACE
    )
    sql, params = ch.query_all_calls[0]
    assert "status = 'conflicted'" in sql
    assert "tombstoned_at IS NULL" in sql
    assert "version < {bv:UInt64}" in sql
    assert "ORDER BY version DESC" in sql
    assert params["bv"] == 1000
    assert params["limit"] == 50


@pytest.mark.asyncio
async def test_lookup_target_returns_none_when_missing() -> None:
    ch = StubCH()
    ch.query_one_returns.append(None)
    out = await lookup_target("clickhouse", "ns", "11111111-2222-3333-4444-555555555555", ch=ch)
    assert out is None
    sql, _ = ch.query_one_calls[0]
    assert "tombstoned_at IS NULL" in sql


@pytest.mark.asyncio
async def test_tombstone_ids_empty_input_short_circuits() -> None:
    """Defensive: an empty IN list would tombstone every row in the
    table. Empty input must NOT touch CH."""
    ch = StubCH()
    await tombstone_ids(
        "clickhouse", [], "note", datetime(2026, 5, 17, tzinfo=UTC), namespace="ns", ch=ch
    )
    assert ch.execute_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trace", "written"),
    [
        ("old", "old | resolve:revise:reason:by"),
        # A live row an older build restored keeps memory.delete_version's marker; the
        # resolution's tombstone does not carry it.
        ("memory.delete_version:42", "resolve:revise:reason:by"),
    ],
)
async def test_tombstone_ids_writes_rmt_versions(
    monkeypatch: pytest.MonkeyPatch, trace: str, written: str
) -> None:
    from server.memory import store as memory_repo
    from server.memory.store import lifecycle

    ch = StubCH()
    captured: dict[str, Any] = {}

    async def fake_get_by_id(mid, *, org=None, namespace=None, ch=None, scope=None):
        return memory_repo.MemoryRow(
            memory_id=str(mid),
            org=org or "o",
            namespace="ns",
            agent="agent",
            user_id="-",
            session_id="-",
            memory_key="k",
            memory_type="semantic",
            value="value",
            confidence=1.0,
            version=1,
            pinned=False,
            created_at=datetime(2026, 5, 17),
            updated_at=datetime(2026, 5, 17),
            expires_at=None,
            source_trace=trace,
        )

    monkeypatch.setattr(lifecycle, "get_by_id", fake_get_by_id)

    async def fake_write_versions_by_ids(
        org, memory_ids, update, *, ch=None, namespace=None, skip=None, strong=False
    ):
        row = memory_repo.MemoryRow(
            memory_id="11111111-2222-3333-4444-555555555555",
            org=org,
            namespace="ns",
            agent="agent",
            user_id="-",
            session_id="-",
            memory_key="k",
            memory_type="semantic",
            value="value",
            confidence=1.0,
            version=1,
            pinned=False,
            created_at=datetime(2026, 5, 17),
            updated_at=datetime(2026, 5, 17),
            expires_at=None,
            source_trace=trace,
        )
        captured["org"] = org
        captured["ids"] = memory_ids
        captured["update"] = update(row)
        return len(memory_ids)

    monkeypatch.setattr(lifecycle, "write_versions_by_ids", fake_write_versions_by_ids)
    await tombstone_ids(
        "clickhouse",
        ["id-1", "id-2"],
        "resolve:revise:reason:by",
        datetime(2026, 5, 17, tzinfo=UTC),
        namespace="ns",
        ch=ch,
    )
    assert captured["org"] == "clickhouse"
    assert captured["ids"] == ["id-1", "id-2"]
    assert captured["update"]["status"] == "tombstoned"
    assert captured["update"]["source_trace"] == written


@pytest.mark.asyncio
async def test_mark_active_by_ids_empty_input_skips_ch() -> None:
    ch = StubCH()
    await mark_active_by_ids("clickhouse", [], namespace="ns", ch=ch)
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_lookup_keys_for_ids_returns_dict() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {"memory_id": "id-1", "memory_key": "alpha"},
            {"memory_id": "id-2", "memory_key": "beta"},
        ]
    )
    out = await lookup_keys_for_ids("clickhouse", ["id-1", "id-2"], namespace="ns", ch=ch)
    assert out == {"id-1": "alpha", "id-2": "beta"}


@pytest.mark.asyncio
async def test_lookup_keys_for_ids_empty_skips_ch() -> None:
    ch = StubCH()
    out = await lookup_keys_for_ids("clickhouse", [], namespace="ns", ch=ch)
    assert out == {}
    assert ch.query_all_calls == []


@pytest.mark.asyncio
async def test_allowlist_list_orders_by_recency() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await allowlist_list("clickhouse", "ns", limit=25, ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "ORDER BY allowed_at DESC" in sql
    assert "tombstoned_at IS NULL" in sql
    assert params["lim"] == 25


@pytest.mark.asyncio
async def test_allowlist_exists_returns_true_when_row_present() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"1": 1})
    assert await allowlist_exists("clickhouse", "ns", "a", "b", ch=ch) is True
    ch.query_one_returns.append(None)
    assert await allowlist_exists("clickhouse", "ns", "a", "b", ch=ch) is False


@pytest.mark.asyncio
async def test_allowlist_insert_skips_empty() -> None:
    ch = StubCH()
    await allowlist_insert([], ch=ch)
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_allowlist_remove_emits_soft_delete_mutation() -> None:
    ch = StubCH()
    await allowlist_remove(
        "clickhouse",
        "ns",
        "a",
        "b",
        "unallow:x",
        datetime(2026, 5, 17, tzinfo=UTC),
        ch=ch,
    )
    sql, params = ch.execute_calls[0]
    assert "ALTER TABLE loom.conflict_allowlist" in sql
    assert "tombstoned_at = {ts:DateTime}" in sql
    assert "reason = concat(reason, ' | ', {note:String})" in sql
    assert params["a"] == "a"
    assert params["note"] == "unallow:x"
