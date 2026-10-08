"""Blacklist precedence and the cross-replica watermark. CH-free.

When patterns of several scopes match one write, the refusal cites the most
specific one: namespace, then org, then global (oldest first within a scope).
Every add/remove moves the watermark, even two that race, so a replica that
refreshed between them still sees the second.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

import server.memory.app.blacklist_guard as bl
from tests.unit.memory_store import MemoryStore, StoreClient


def _pattern(pid: str, scope: str, *, org: str = "o", ns: str = "ns", minute: int = 0) -> dict:
    return {
        "pattern_id": pid,
        "org": "*" if scope == "global" else org,
        "namespace": ns if scope == "namespace" else "*",
        "pattern": "secret",
        "pattern_type": "substring",
        "scope": scope,
        "reason": f"{scope} reason",
        "created_at": datetime(2026, 1, 1, 0, minute),
        "tombstoned_at": None,
    }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    s = MemoryStore()
    client = StoreClient(s)
    monkeypatch.setattr(bl, "get_client", lambda: client)
    monkeypatch.setattr(bl, "_dirty", True)
    monkeypatch.setattr(bl, "_cache", [])
    monkeypatch.setattr(bl.settings, "blacklist_version_check_interval_seconds", 0.0)
    return s


@pytest.mark.parametrize(
    "rows,expected",
    [
        # Inserted oldest-first in the least specific order: global, org, namespace.
        ([("g", "global", 0), ("org", "org", 1), ("n", "namespace", 2)], "n"),
        ([("org", "org", 0), ("g", "global", 1)], "org"),
        ([("g1", "global", 0), ("g2", "global", 1)], "g1"),
        ([("n2", "namespace", 5), ("g", "global", 0), ("n1", "namespace", 1)], "n1"),
    ],
)
async def test_the_most_specific_scope_wins(
    store: MemoryStore, rows: list[tuple[str, str, int]], expected: str
) -> None:
    ordered = sorted(rows, key=lambda r: r[2])  # the statement orders by created_at
    store.blacklist.extend(_pattern(pid, scope, minute=m) for pid, scope, m in ordered)
    hit = await bl.find_match(org="o", namespace="ns", value="a secret value")
    assert hit is not None and hit.pattern_id == expected


async def test_another_namespace_pattern_does_not_apply(store: MemoryStore) -> None:
    store.blacklist.extend(
        [_pattern("other", "namespace", ns="elsewhere"), _pattern("org", "org", minute=1)]
    )
    hit = await bl.find_match(org="o", namespace="ns", value="secret")
    assert hit is not None and hit.pattern_id == "org"


async def test_two_racing_bumps_both_move_the_watermark(store: MemoryStore) -> None:
    await bl._bump_version("add:a")
    await bl.find_match(org="o", namespace="ns", value="x")  # a replica loads here
    assert await bl._is_stale() is False
    cached = bl._cached_version
    await asyncio.gather(bl._bump_version("add:b"), bl._bump_version("remove:c"))
    assert len(set(store.blacklist_versions)) == 3  # no two bumps wrote the same version
    assert await bl._current_version() != cached
    assert await bl._is_stale() is True


async def test_the_watermark_is_read_before_the_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """add/remove writes its row and then bumps. Reading the watermark first
    means a bump the refresh missed leaves the cache behind (the next check
    refreshes); reading it after the rows could cache the new watermark with
    the old rows."""
    order: list[str] = []

    class _CH:
        async def query_one(self, sql: str, *a: Any, **k: Any) -> dict:
            order.append("watermark")
            return {"n": 1, "s": 1}

        async def query_all(self, sql: str, *a: Any, **k: Any) -> list:
            order.append("rows")
            return []

    monkeypatch.setattr(bl, "get_client", lambda: _CH())
    await bl._refresh_cache()
    assert order == ["watermark", "rows"]
