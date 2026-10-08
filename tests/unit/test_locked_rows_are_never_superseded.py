"""A lock is a write-time invariant: another write's refines / newest_wins /
repair retirement never supersedes a locked row (read_only refuses any
change, no_delete refuses consolidate-away). CH-free."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

import server.memory.store.lifecycle as lc
from server.memory.domain.lock import refuses_retirement
from server.memory.store.rows import MemoryRow


def _row(mid: str, **kw: Any) -> MemoryRow:
    return MemoryRow(
        memory_id=mid,
        org="o",
        namespace="ns",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key=f"k-{mid}",
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=7,
        pinned=False,
        created_at=datetime(2026, 9, 24),
        expires_at=None,
        **kw,
    )


class _CH:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserted.append((table, rows))


@pytest.mark.asyncio
async def test_supersede_leaves_a_locked_victim_live_in_both_tables(monkeypatch) -> None:
    heads = {
        "free": _row("free"),
        "ro": _row("ro", lock_mode="read_only"),
        "nd": _row("nd", lock_mode="no_delete"),
        "lapsed": _row("lapsed", lock_mode="read_only", lock_until=datetime(2000, 1, 1)),
    }

    async def _get(mid: str, *, org: str, ch: Any = None) -> MemoryRow | None:
        return heads.get(mid)

    embedded: list[str] = []

    async def _live(ch: Any, **kw: Any) -> list[dict[str, Any]]:
        embedded.append(kw["memory_key"])
        return []

    async def _mirror(rows: Any, *, ch: Any = None) -> None:
        return None

    monkeypatch.setattr(lc, "get_by_id", _get)
    monkeypatch.setattr(lc, "live_embedding_rows_for_key", _live)
    monkeypatch.setattr(lc, "mirror_status_to_embeddings", _mirror)
    victims = [
        {
            "memory_id": mid,
            "namespace": "ns",
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "memory_key": f"k-{mid}",
            "superseded_by": "new",
        }
        for mid in heads
    ]
    ch = _CH()
    n = await lc.supersede_duplicates("o", victims, ch=ch, skip=refuses_retirement)
    written = {r["memory_id"] for _, rows in ch.inserted for r in rows}
    assert written == {"free", "lapsed"} and n == 2
    assert sorted(embedded) == ["k-free", "k-lapsed"]  # locked rows stay live in recall too


def test_refuses_retirement_follows_the_lock_in_force() -> None:
    later = datetime.now() + timedelta(days=1)
    assert refuses_retirement(_row("a", lock_mode="read_only"))
    assert refuses_retirement(_row("b", lock_mode="no_delete", lock_until=later))
    assert not refuses_retirement(_row("c"))
    assert not refuses_retirement(_row("d", lock_mode="no_delete", lock_until=datetime(2000, 1, 1)))
