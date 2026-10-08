"""Unit tests for server/identity/store/grants.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.store.grants import find_active_grant, insert_row, list_active


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


@pytest.mark.asyncio
async def test_insert_row_forces_synchronous_visibility() -> None:
    ch = StubCH()
    await insert_row(
        {
            "org": "clickhouse",
            "namespace": "ns",
            "subject": "alice",
            "role": "writer",
            "granted_at": datetime(2026, 5, 17, tzinfo=UTC),
            "granted_by": "admin",
            "revoked_at": None,
            "version": 1,
        },
        ch=ch,
    )
    table, _, settings = ch.insert_calls[0]
    assert table == "loom.namespace_grants"
    assert settings.get("async_insert") == 0


@pytest.mark.asyncio
async def test_list_active_excludes_revoked() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "alice",
                "role": "writer",
                "granted_at": datetime(2026, 5, 1, tzinfo=UTC),
                "granted_by": "admin",
            },
        ]
    )
    rows = await list_active("clickhouse", "ns", limit=50, ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "revoked_at IS NULL" in sql
    assert "ORDER BY granted_at DESC" in sql
    assert params["lim"] == 50
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_find_active_grant_filters_to_subject() -> None:
    """revoke flow: look up the live grant by (org, ns, subject), use
    its role + granted_by to write a matching revoke row."""
    ch = StubCH()
    ch.query_one_returns.append({"role": "writer", "granted_by": "admin"})
    out = await find_active_grant("clickhouse", "ns", "alice", ch=ch)
    sql, params = ch.query_one_calls[0]
    assert "subject = {sub:String}" in sql
    assert "revoked_at IS NULL" in sql
    assert params["sub"] == "alice"
    assert out is not None and (out.role, out.granted_by) == ("writer", "admin")


@pytest.mark.asyncio
async def test_find_active_grant_returns_none_when_revoked() -> None:
    """If the live row is gone, revoke returns False without writing."""
    ch = StubCH()
    ch.query_one_returns.append(None)
    out = await find_active_grant("clickhouse", "ns", "alice", ch=ch)
    assert out is None
