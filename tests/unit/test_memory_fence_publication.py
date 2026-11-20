"""A refused value insert publishes no searchable text."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest

from server.core.lease_writes import FenceUnavailableError
from server.memory import store
from server.memory.app import bulk_set, set_commit, update
from server.memory.store.rows import identity_key
from server.memory.store.write_fence import lease_name
from tests.unit.test_memory_verify import _row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["set", "set_high", "update", "update_retire", "update_high", "bulk_set", "bulk_high"],
)
async def test_contended_value_write_publishes_no_embedding(
    operation, memory_insert_keeper, monkeypatch
):
    source = _row()
    memory = store.build_memory_version_row(source)
    embedding = {"memory_key": source.memory_key, "excerpt": "uncommitted value"}
    inserted: dict[str, list[dict[str, Any]]] = {}
    retire = AsyncMock()
    high_fence = AsyncMock()
    monkeypatch.setattr(store, "tombstone_vectors_of", retire)
    monkeypatch.setattr(update, "fence_high", high_fence)
    monkeypatch.setattr(set_commit, "fence_high", high_fence)
    monkeypatch.setattr(bulk_set, "fence_high", high_fence)
    announce = AsyncMock()
    monkeypatch.setattr(bulk_set, "_bulk_announce", announce)

    class CH:
        async def insert(self, table, rows, **kw):
            inserted.setdefault(table, []).extend(rows)

    memory_insert_keeper.seed(lease_name(identity_key(source)), "another-writer")
    ch = CH()
    with pytest.raises(FenceUnavailableError):
        if operation == "set":
            await set_commit._insert_row_and_vector(
                SimpleNamespace(memory_row=memory, embedder=SimpleNamespace(dim=4)), ch, embedding
            )
        elif operation == "set_high":
            await set_commit._land_plain_row(SimpleNamespace(memory_row=memory), ch, high=True)
        elif operation == "bulk_high":
            await bulk_set._land_batch(
                ch, SimpleNamespace(memory_rows=[memory], embedding_rows=[]), None, {}
            )
        elif operation in ("update", "update_retire", "update_high"):
            await update._land_inserted(
                source,
                memory,
                [] if operation == "update_retire" else [embedding],
                ch,
                retire_vectors=operation == "update_retire",
                high=operation == "update_high",
                record=AsyncMock(),
            )
        else:
            await bulk_set._insert_batch(
                SimpleNamespace(memory_rows=[memory], embedding_rows=[embedding]), ch
            )
    assert inserted == {}, "a refused write must never be searchable"
    retire.assert_not_awaited()
    high_fence.assert_not_awaited()
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_admission_leaves_search_data_live(monkeypatch):
    import asyncio

    from server.core import leases

    entered = asyncio.Event()
    fence = AsyncMock()
    monkeypatch.setattr(update, "fence_high", fence)

    async def taking(*args, **kw):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(leases, "take", taking)
    source = _row()
    task = asyncio.create_task(
        update._land_inserted(
            source,
            store.build_memory_version_row(source, sensitivity="high"),
            [],
            object(),
            retire_vectors=True,
            high=True,
            record=AsyncMock(),
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fence.assert_not_awaited()


@pytest.mark.asyncio
async def test_unanswered_high_memory_send_still_fences_search(monkeypatch):
    fence = AsyncMock()
    monkeypatch.setattr(update, "fence_high", fence)
    source = _row()
    memory = store.build_memory_version_row(source, sensitivity="high")

    class LostAnswer:
        async def insert(self, *args, **kw):
            raise TimeoutError("the memory insert may have committed")

    with pytest.raises(TimeoutError):
        await update._land_inserted(
            source, memory, [], LostAnswer(), retire_vectors=True, high=True, record=AsyncMock()
        )
    fence.assert_awaited_once_with([(identity_key(source), memory["version"])], ch=ANY)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "bulk"])
async def test_high_write_cancelled_during_holder_release_still_fences_search(
    monkeypatch, operation
):
    import asyncio

    from server.core import leases

    releasing, finish = asyncio.Event(), asyncio.Event()
    release = leases.release_shielded
    fence = AsyncMock()
    monkeypatch.setattr(set_commit, "fence_high", fence)
    monkeypatch.setattr(bulk_set, "fence_high", fence)
    announce = AsyncMock()
    monkeypatch.setattr(bulk_set, "_bulk_announce", announce)
    source = _row()
    memory = store.build_memory_version_row(source, sensitivity="high")
    inserted = []

    class CH:
        async def insert(self, table, batch, **kw):
            inserted.extend(batch)

    async def held_release(lease):
        releasing.set()
        await finish.wait()
        await release(lease)

    monkeypatch.setattr(leases, "release_shielded", held_release)
    ch = CH()
    call = (
        set_commit._land_plain_row(SimpleNamespace(memory_row=memory), ch, high=True)
        if operation == "set"
        else bulk_set._land_batch(
            ch, SimpleNamespace(memory_rows=[memory], embedding_rows=[]), None, {}
        )
    )
    task = asyncio.create_task(call)
    await releasing.wait()
    assert inserted == [memory]
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    fence.assert_awaited_once_with([(identity_key(source), memory["version"])], ch=ch)
    assert announce.await_count == (operation == "bulk")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["set", "bulk"])
async def test_cancelled_high_send_fences_and_preserves_the_cancellation(monkeypatch, operation):
    import asyncio

    original = asyncio.CancelledError("memory send cancelled")
    fence = AsyncMock()
    monkeypatch.setattr(set_commit, "fence_high", fence)
    monkeypatch.setattr(bulk_set, "fence_high", fence)
    announce = AsyncMock()
    monkeypatch.setattr(bulk_set, "_bulk_announce", announce)
    memory = store.build_memory_version_row(_row(), sensitivity="high")

    class CH:
        async def insert(self, *args, **kw):
            raise original

    ch = CH()
    with pytest.raises(asyncio.CancelledError) as caught:
        if operation == "set":
            await set_commit._land_plain_row(SimpleNamespace(memory_row=memory), ch, high=True)
        else:
            await bulk_set._land_batch(
                ch, SimpleNamespace(memory_rows=[memory], embedding_rows=[]), None, {}
            )
    assert caught.value is original
    fence.assert_awaited_once_with([(identity_key(memory), memory["version"])], ch=ch)
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_copy_commit_mark_and_followups_survive_holder_release_cancellation(monkeypatch):
    import asyncio

    from server.core import leases

    releasing, finish = asyncio.Event(), asyncio.Event()
    release = leases.release_shielded
    calls = []
    memory = store.build_memory_version_row(_row())

    class CH:
        async def insert(self, *args, **kw):
            return None

    async def held_release(lease):
        releasing.set()
        await finish.wait()
        await release(lease)

    async def landed():
        calls.append("landed")

    async def then():
        calls.append("then")

    monkeypatch.setattr(leases, "release_shielded", held_release)
    task = asyncio.create_task(
        store.insert_versions_over_heads(
            [memory],
            [_row()],
            ch=CH(),
            on_commit=lambda: calls.append("committed"),
            landed=landed,
            then=then,
        )
    )
    await releasing.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["committed", "landed", "then"]
