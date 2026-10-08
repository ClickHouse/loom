"""A write that did not land at its key, and a lock that found
no row, say so.

A set can merge the same content into ANOTHER key (no row at the asked key);
a lock that then answers 200 locked:false sends the follow-up write to a key
that was never locked. An update can answer 200 updated:false because its key
resolution missed a row written moments earlier on a lagging replica.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.api.models_crud import MemoryUpdateInput
from server.memory.api.models_lock import MemoryLockInput, MemoryUnlockInput
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from server.memory.app.lock import handle_memory_lock, handle_memory_unlock
from server.memory.domain.conflicts import ConflictResult
from tests.unit.test_memory_set_verdict import _PEER_ID, _existing_row, _input, _peer, _wire

_ID = {"org": "test", "namespace": "personal", "agent": "cursor"}


@pytest.mark.asyncio
async def test_a_merge_into_another_key_names_that_key(monkeypatch: pytest.MonkeyPatch) -> None:
    other = _existing_row().model_copy(update={"memory_key": "test/lockrace/update/0"})
    for dry in (False, True):
        _wire(
            monkeypatch,
            ConflictResult(
                verdict="merge", peers=[_peer(similarity=0.99)], existing_memory_id=_PEER_ID
            ),
            existing_by_id=other,
        )
        inp = _input(dry_run=dry)
        out = await write_mod.handle_memory_set(inp)
        assert out.conflict_verdict == "merge" and out.noop is True
        assert out.memory_key == inp.memory_key
        assert out.merged_into_key == "test/lockrace/update/0"


@pytest.mark.asyncio
async def test_a_merge_into_the_same_key_leaves_merged_into_key_empty(monkeypatch) -> None:
    _wire(
        monkeypatch,
        ConflictResult(
            verdict="merge", peers=[_peer(similarity=0.99)], existing_memory_id=_PEER_ID
        ),
        existing_by_id=_existing_row().model_copy(update={"memory_key": _input().memory_key}),
    )
    out = await write_mod.handle_memory_set(_input())
    assert out.merged_into_key == ""


@pytest.mark.asyncio
async def test_a_lock_or_unlock_with_no_row_says_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr("server.memory.store.get_by_key", _none)
    out = await handle_memory_lock(MemoryLockInput(**_ID, memory_key="k", reason="r"))
    assert out.locked is False and out.reason == "not_found"
    un = await handle_memory_unlock(MemoryUnlockInput(**_ID, memory_key="k", reason="r"))
    assert un.unlocked is False and un.reason == "not_found"


def test_key_resolution_retries_consistently_on_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lagging replica answers the plain read empty; only the
    sequentially consistent retry sees the row written a moment ago."""
    sqls: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            sqls.append(sql)
            if "select_sequential_consistency = 1" in sql:
                return [
                    {
                        "memory_id": "m",
                        "agent": "a",
                        "user_id": "-",
                        "session_id": "-",
                        "version": 2,
                        "status": "active",
                    }
                ]
            return []

    rows = asyncio.run(
        update_mod._live_rows_for_key(MemoryUpdateInput(**_ID, memory_key="k", value="v"), _CH())
    )
    assert [r["memory_id"] for r in rows] == ["m"]
    assert len(sqls) == 2 and "select_sequential_consistency = 1" in sqls[1]
