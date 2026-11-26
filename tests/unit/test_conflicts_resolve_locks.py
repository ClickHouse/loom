"""conflicts.resolve never tombstones a locked memory.

A lock refuses `memory.delete` (423 LOCKED), and a resolve refuses the same row. It
tombstones the side that loses, so it reads those heads current and asserts their locks
before the first write: a refused resolve leaves every row as it was. `allow` tombstones
nothing and settles a locked pair, and a lapsed lock is no lock.

The handler, `conflicts_repo.tombstone_ids` and `write_versions_by_ids` are the real
ones; the reads and the two inserts are faked, as in test_tombstone_cascade.py.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory import store as memory_repo
from server.memory.api.models_conflicts import ConflictsResolveInput
from server.memory.app import conflicts_ops as h
from server.memory.domain import lock
from server.memory.store import conflicts as conflicts_repo
from server.memory.store import lifecycle

NEW = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"
DRAFT = "33333333-3333-3333-3333-333333333333"


def _row(mid: str, key: str, peer: str, **over: Any) -> memory_repo.MemoryRow:
    return memory_repo.MemoryRow(
        memory_id=mid,
        org="acme",
        namespace="fleet-a",
        agent="a",
        user_id="-",
        session_id="s1",
        memory_key=key,
        memory_type="semantic",
        value="v",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 1),
        updated_at=datetime(2026, 9, 1),
        expires_at=None,
        source_trace="",
        status="conflicted",
        conflict_with=[peer],
        **over,
    )


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """A conflicted pair whose older row is locked; returns the versions written."""
    rows = {
        NEW: _row(NEW, "deploy_day_new", OLD),
        OLD: _row(OLD, "deploy_day", NEW, lock_reason="keep", locked_by="curator"),
    }
    out: list[dict[str, Any]] = []

    async def get_by_id(mid: str, **kw: Any) -> Any:
        row = rows.get(mid)
        return row if row is not None and row.namespace == kw.get("namespace") else None

    async def insert(built: list[dict[str, Any]], **callbacks: Any) -> None:
        out.extend(built)
        if on_inserted := callbacks.get("on_inserted"):
            on_inserted()

    async def mirror(built: list[dict[str, Any]], **_: Any) -> int:
        return len(built)

    async def lookup(org: str, namespace: str, memory_id: str, **_: Any) -> Any:
        row = rows.get(memory_id)
        if row is None:
            return None
        return conflicts_repo.ConflictTarget.from_row(
            {"memory_id": memory_id, "conflict_with": row.conflict_with, "status": row.status}
        )

    async def count(*_: Any, **__: Any) -> int:
        return 0

    async def keys(org: str, ids: list[str], **_: Any) -> dict[str, str]:
        return {i: rows[i].memory_key for i in ids}

    async def allowlist(pairs: list[dict[str, Any]], **_: Any) -> None:
        return None

    async def announce(events: Any) -> None:
        return None

    monkeypatch.setattr(lifecycle, "get_by_id", get_by_id)
    monkeypatch.setattr("server.memory.store.get_by_id", get_by_id)
    monkeypatch.setattr(lifecycle, "insert_memory_versions", insert)
    monkeypatch.setattr(lifecycle, "mirror_status_to_embeddings", mirror)
    monkeypatch.setattr(lifecycle, "_default_client", lambda: object())
    monkeypatch.setattr(conflicts_repo, "lookup_target", lookup)
    monkeypatch.setattr(conflicts_repo, "count_conflicted", count)
    monkeypatch.setattr(conflicts_repo, "lookup_keys_for_ids", keys)
    monkeypatch.setattr(conflicts_repo, "allowlist_insert", allowlist)
    monkeypatch.setattr(h.webhooks, "announce", announce)
    h.reset_conflicts_count_cache()
    out.append({"rows": rows})  # the fixture's rows, popped by `_lock`
    return out


def _lock(
    written: list[dict[str, Any]], mode: str, *, until: datetime | None = None
) -> dict[str, memory_repo.MemoryRow]:
    """Locks the older row and returns the fixture's rows."""
    rows = written.pop()["rows"]
    rows[OLD] = rows[OLD].model_copy(update={"lock_mode": mode, "lock_until": until})
    assert lock.refuses(rows[OLD], "delete") is (bool(mode) and until is None)
    return rows


async def _resolve(memory_id: str, decision: str) -> Any:
    return await h.handle_conflicts_resolve(
        ConflictsResolveInput(
            org="acme",
            namespace="fleet-a",
            memory_id=memory_id,
            decision=decision,  # type: ignore[arg-type]
        )
    )


@pytest.mark.parametrize("mode", ["read_only", "no_delete"])
@pytest.mark.parametrize(
    ("memory_id", "decision"), [(NEW, "revise"), (OLD, "contract")], ids=["revise", "contract"]
)
async def test_a_resolve_that_would_tombstone_a_locked_row_is_refused(
    written: list[dict[str, Any]], mode: str, memory_id: str, decision: str
) -> None:
    _lock(written, mode)
    with pytest.raises(HTTPException) as refused:
        await _resolve(memory_id, decision)
    assert refused.value.status_code == 423
    assert refused.value.detail["code"] == "LOCKED"
    assert refused.value.detail["memory_key"] == "deploy_day"
    assert written == []  # neither the tombstone nor the winner's status


async def test_one_locked_peer_refuses_the_whole_resolve(written: list[dict[str, Any]]) -> None:
    rows = _lock(written, "no_delete")
    rows[DRAFT] = _row(DRAFT, "deploy_day_draft", NEW)  # unlocked, and read first
    rows[NEW] = rows[NEW].model_copy(update={"conflict_with": [DRAFT, OLD]})
    with pytest.raises(HTTPException) as refused:
        await _resolve(NEW, "revise")
    assert refused.value.status_code == 423
    assert written == []


async def test_a_lapsed_lock_is_no_lock(written: list[dict[str, Any]]) -> None:
    _lock(written, "read_only", until=datetime(2026, 9, 2))
    out = await _resolve(NEW, "revise")
    assert out.resolved
    assert {str(r["memory_id"]): r["status"] for r in written} == {OLD: "tombstoned", NEW: "active"}


async def test_allow_settles_a_locked_pair(written: list[dict[str, Any]]) -> None:
    _lock(written, "read_only")
    out = await _resolve(NEW, "allow")
    assert out.resolved
    assert {str(r["memory_id"]): r["status"] for r in written} == {NEW: "active", OLD: "active"}


async def test_an_unlocked_pair_resolves(written: list[dict[str, Any]]) -> None:
    _lock(written, "")
    out = await _resolve(NEW, "revise")
    assert out.resolved
    assert {str(r["memory_id"]): r["status"] for r in written} == {OLD: "tombstoned", NEW: "active"}
