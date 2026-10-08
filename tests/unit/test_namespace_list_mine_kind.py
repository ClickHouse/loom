"""list_mine: claim-only (JWT) namespaces carry the directory's kind/visibility
instead of hard-coded memory/private, so code namespaces named in a token's
claim never masquerade as a memory identity."""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.app import namespace_membership as ns_mod


class _CH:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append(params or {})
        return self.rows


@pytest.mark.asyncio
async def test_namespace_meta_maps_kind_and_visibility() -> None:
    ch = _CH(
        [
            {
                "namespace": "code-clickhouse",
                "kind": "code",
                "visibility": "public",
                "description": "Code index",
            },
            {"namespace": "team-kb", "kind": "memory", "visibility": "private", "description": ""},
        ]
    )
    meta = await ns_mod._namespace_meta(ch, "clickhouse", ["code-clickhouse", "team-kb", "ghost"])
    assert meta["code-clickhouse"] == {
        "kind": "code",
        "visibility": "public",
        "description": "Code index",
    }
    assert meta["team-kb"]["kind"] == "memory"
    assert "ghost" not in meta  # no row → caller falls back to memory/private
    assert ch.calls[0]["ns"] == ["code-clickhouse", "team-kb", "ghost"]


@pytest.mark.asyncio
async def test_namespace_meta_fails_open() -> None:
    class _Boom:
        async def query_all(self, sql, params=None):
            raise RuntimeError("ch down")

    assert await ns_mod._namespace_meta(_Boom(), "o", ["x"]) == {}
