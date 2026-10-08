"""The entity rescan backlog is fair across namespaces and
oldest first, so a large namespace cannot starve the others' old rows."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.workers.entity_extractor import interleave_by_namespace
from server.settings import settings


def _k(ns: str, key: str) -> dict[str, str]:
    return {
        "org": "o",
        "namespace": ns,
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
    }


def test_namespaces_take_turns_and_keep_their_own_order() -> None:
    keys = [_k("big", f"b{i}") for i in range(4)] + [
        _k("small", "s0"),
        _k("small", "s1"),
        _k("tiny", "t0"),
    ]
    out = [(k["namespace"], k["memory_key"]) for k in interleave_by_namespace(keys)]
    assert out[:3] == [("big", "b0"), ("small", "s0"), ("tiny", "t0")]
    assert out[3:5] == [("big", "b1"), ("small", "s1")]
    assert [k for ns, k in out if ns == "big"] == ["b0", "b1", "b2", "b3"]
    assert len(out) == len(keys)
    assert interleave_by_namespace([]) == []


class _CH:
    def __init__(self) -> None:
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.sql, self.params = sql, params or {}
        return []


@pytest.mark.asyncio
async def test_the_backlog_read_is_oldest_first_and_capped_per_namespace() -> None:
    ch = _CH()
    await memory_repo.find_unentitied_keys(batch_size=5000, per_namespace=500, ch=ch)
    assert "ORDER BY argMax(m.created_at, version) ASC" in ch.sql
    assert "LIMIT {per_ns:UInt32} BY org, namespace" in ch.sql and ch.params["per_ns"] == 500


@pytest.mark.asyncio
async def test_the_windowed_read_is_unchanged() -> None:
    ch = _CH()
    await memory_repo.find_unentitied_keys(batch_size=25, since_seconds=7200, ch=ch)
    assert (
        "ORDER BY argMax(m.created_at, version) DESC" in ch.sql
        and "LIMIT {per_ns:UInt32} BY" not in ch.sql
    )


def test_default_share() -> None:
    assert settings.entity_extract_backlog_per_namespace == 500
