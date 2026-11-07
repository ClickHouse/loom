"""A lock/pin guard decides on the LATEST head.

An update that reads the pre-lock version from a lagging replica passes the
guard and writes its new version from that head, erasing the lock. Every read
that feeds a memory-row write reads with sequential consistency
(`strong=True`). The stub below is the lagging
replica: a plain read answers the pre-lock head, only a strong read the lock.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

import server.memory.app.write as _mw
from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemorySetInput,
    MemoryTouchInput,
    MemoryUpdateByIdInput,
    MemoryUpdateInput,
)
from server.memory.api.models_lock import MemoryLockInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import set_commit as set_commit
from server.memory.app import stack as _stack_mod
from server.memory.app import update as update_mod
from server.memory.app import write as write_mod
from server.memory.app.lock import handle_memory_lock
from tests.unit.memory_store import one_holder
from tests.unit.test_memory_lock import _CH, _ID, _row


def _lagging_replica(monkeypatch: pytest.MonkeyPatch) -> tuple[_CH, list[dict[str, Any]]]:
    ch = _CH()
    stale = _row()  # the version before the lock
    locked = _row(version=101, lock_mode="read_only", lock_reason="hold", locked_by="human:o")
    reads: list[dict[str, Any]] = []
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _read(*_: Any, **kw: Any) -> Any:
        reads.append(kw)
        return locked if kw.get("strong") else stale

    monkeypatch.setattr("server.memory.store.get_by_key", _read)
    monkeypatch.setattr("server.memory.store.get_by_id", _read)
    monkeypatch.setattr("server.memory.store.namespaces_of_id", one_holder)

    async def _one_live_row(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return [
            {
                "memory_id": stale.memory_id,
                "agent": "cursor",
                "user_id": "-",
                "session_id": "-",
                "version": 101,
                "status": "active",
            }
        ]

    monkeypatch.setattr(update_mod, "_live_rows_for_key", _one_live_row)
    return ch, reads


async def _refused(coro: Any, action: str) -> None:
    with pytest.raises(HTTPException) as e:
        await coro
    assert e.value.status_code == 423, e.value.detail
    assert e.value.detail["refused_action"] == action


@pytest.mark.asyncio
async def test_update_on_a_lagging_replica_still_sees_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, reads = _lagging_replica(monkeypatch)
    await _refused(
        update_mod.handle_memory_update(
            MemoryUpdateInput(**_ID, memory_key="deploy_window", value="Fridays too")
        ),
        "value",
    )
    await _refused(
        update_mod.handle_memory_update_by_id(
            MemoryUpdateByIdInput(org="test", memory_id=_row().memory_id, value="Fridays too")
        ),
        "value",
    )
    assert ch.inserts == [] and reads and all(r.get("strong") for r in reads)


@pytest.mark.asyncio
async def test_set_delete_touch_on_a_lagging_replica_still_see_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch, reads = _lagging_replica(monkeypatch)
    await _refused(
        write_mod.handle_memory_set(
            MemorySetInput(**_ID, memory_key="deploy_window", value="Fridays too", classify=False)
        ),
        "value",
    )
    await _refused(
        mem_handlers.handle_memory_delete(MemoryDeleteInput(**_ID, memory_key="deploy_window")),
        "delete",
    )
    await _refused(
        mem_handlers.handle_memory_touch(
            MemoryTouchInput(**_ID, memory_key="deploy_window", ttl_seconds=60)
        ),
        "expire",
    )
    assert ch.inserts == []
    # Unfixed, delete read fresh-on-miss only: the stale head passed the guard
    # and the locked row was tombstoned.
    assert reads and all(r.get("strong") for r in reads), reads


@pytest.mark.asyncio
async def test_a_relock_builds_on_the_latest_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock written from a stale head would carry the stale value forward
    and erase whatever version came after it; the lock reads strong too."""
    ch, reads = _lagging_replica(monkeypatch)
    await handle_memory_lock(
        MemoryLockInput(**_ID, memory_key="deploy_window", mode="read_only", reason="tighter")
    )
    assert reads[-1].get("strong") is True
    assert ch.inserts and ch.inserts[0][1][0]["version"] != 100


def test_every_write_feeding_head_read_is_strong() -> None:
    """Structural: the head reads of the guarded write paths all pass
    strong=True (a new write path that forgets it fails here)."""
    import inspect
    import re

    from server.memory.app import apply as memory_apply
    from server.memory.app import curation as _memory_curation
    from server.memory.app import dedup as memory_dedup
    from server.memory.app import handlers as memory
    from server.memory.app import lock as _memory_lock
    from server.memory.app import move as memory_move
    from server.memory.app import restore as restore_mod
    from server.memory.app import update as update_mod

    def calls(fn: Any) -> list[str]:
        src = inspect.getsource(fn)
        return re.findall(r"get_by_(?:key|id)\((?:[^()]|\([^()]*\))*\)", src, flags=re.S)

    guarded = [
        _mw._set_fetch_existing,
        _mw._set_check_if_match,
        set_commit._set_merge,
        memory.handle_memory_delete,
        memory._delete_version_locked,
        memory.handle_memory_touch,
        update_mod.handle_memory_update,
        update_mod.handle_memory_update_by_id,
        restore_mod._read_restorable,
        restore_mod._status_before_retirement,
        _memory_lock._head,
        _memory_curation.handle_memory_redact,
        memory_apply.resolve,
        memory_move._load_source,
        memory_move._destination_id,
        memory_dedup.handle_memory_dedup_resolve,
    ]
    for fn in guarded:
        found = calls(fn)
        assert found, fn.__name__
        weak = [c for c in found if "strong=True" not in c and "strong=strong" not in c]
        assert not weak, (fn.__name__, weak)


@pytest.mark.asyncio
async def test_a_write_right_after_unlock_sees_the_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: a delete right after memory.unlock must not
    answer 423 from the pre-unlock head. The lagging replica serves the
    LOCKED version; only a strong read sees the unlock."""
    ch = _CH()
    locked = _row(lock_mode="read_only", lock_reason="hold", locked_by="human:o")
    unlocked = _row(version=102)
    reads: list[dict[str, Any]] = []
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)

    async def _read(*_: Any, **kw: Any) -> Any:
        reads.append(kw)
        return unlocked if kw.get("strong") else locked

    monkeypatch.setattr("server.memory.store.get_by_key", _read)
    monkeypatch.setattr("server.memory.store.get_by_id", _read)
    monkeypatch.setattr("server.memory.store.namespaces_of_id", one_holder)
    out = await mem_handlers.handle_memory_delete(
        MemoryDeleteInput(**_ID, memory_key="deploy_window")
    )
    assert out.tombstoned is True
    assert reads and all(r.get("strong") for r in reads)
