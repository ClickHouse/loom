"""A refused value insert publishes no searchable text."""

from __future__ import annotations

import asyncio
import functools
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY, AsyncMock

import pytest

from server.core.lease_writes import FenceUnavailableError
from server.memory import store
from server.memory.api.models_crud import MemorySetInput
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
    receipt, point_announce, revision = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(set_commit, "_audit_set_receipt", receipt)
    monkeypatch.setattr(set_commit, "_announce_set", point_announce)
    monkeypatch.setattr(store, "tombstone_vectors_of", retire)
    monkeypatch.setattr(update, "fence_high", high_fence)
    monkeypatch.setattr(set_commit, "fence_high", high_fence)
    monkeypatch.setattr(bulk_set, "fence_high", high_fence)
    announce = AsyncMock()
    monkeypatch.setattr(bulk_set, "_bulk_announce", announce)

    class CH:
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

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
                record=revision,
            )
        else:
            await bulk_set._insert_batch(
                SimpleNamespace(memory_rows=[memory], embedding_rows=[embedding]), ch
            )
    assert inserted == {}, "a refused write must never be searchable"
    retire.assert_not_awaited()
    high_fence.assert_not_awaited()
    announce.assert_not_awaited()
    receipt.assert_not_awaited()
    point_announce.assert_not_awaited()
    revision.assert_not_awaited()


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
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return []

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
    monkeypatch.setattr(set_commit, "_receipt_for_set", AsyncMock())
    monkeypatch.setattr(set_commit, "_lift_after_insert", AsyncMock(return_value=100))
    monkeypatch.setattr(set_commit, "_announce_set", AsyncMock())
    monkeypatch.setattr(store, "tombstone_vectors_of", AsyncMock())
    announce = AsyncMock()
    monkeypatch.setattr(bulk_set, "_bulk_announce", announce)
    source = _row()
    memory = store.build_memory_version_row(source, sensitivity="high")
    inserted = []

    class CH:
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

        async def insert(self, table, batch, **kw):
            inserted.extend(batch)

    async def held_release(lease):
        releasing.set()
        await finish.wait()
        await release(lease)

    monkeypatch.setattr(leases, "release_shielded", held_release)
    ch = CH()
    call = (
        set_commit._land_plain_row(
            SimpleNamespace(memory_row=memory, existing=source), ch, high=True
        )
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
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return []

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
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

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


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["plain_set", "searchable_set", "update"])
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_acknowledged_point_write_records_after_release_cancellation(
    monkeypatch, operation, cancel_again
):
    import asyncio

    from server.core import leases
    from server.memory.app import conflicts

    source = _row()
    memory = store.build_memory_version_row(source)
    w = set_commit._SetWrite(
        input=MemorySetInput(
            org=source.org,
            namespace=source.namespace,
            agent=source.agent,
            memory_key=source.memory_key,
            value=source.value,
        ),
        version=source.version,
        retyped_from="",
        existing=source,
        expires_at=None,
        memory_row=memory,
        memory_id=source.memory_id,
        embedder=SimpleNamespace(model_name="m", dim=4),
    )
    following, finish_following = asyncio.Event(), asyncio.Event()
    completed = []
    pause_at = {"plain_set": "receipt", "searchable_set": "announce", "update": "revision"}

    async def follow(name, *args, **kw):
        if name == pause_at[operation]:
            following.set()
            await finish_following.wait()
        completed.append(name)

    receipt = AsyncMock(side_effect=functools.partial(follow, "receipt"))
    announce = AsyncMock(side_effect=functools.partial(follow, "announce"))
    revision = AsyncMock(side_effect=functools.partial(follow, "revision"))
    monkeypatch.setattr(set_commit, "_audit_set_receipt", receipt)
    monkeypatch.setattr(set_commit, "_announce_set", announce)
    monkeypatch.setattr(set_commit, "_lift_above_deletes", AsyncMock())
    monkeypatch.setattr(set_commit, "_lift_after_insert", AsyncMock(return_value=source.version))
    monkeypatch.setattr(store, "tombstone_vectors_of", AsyncMock())
    monkeypatch.setattr(store, "search_document_row", lambda *a, **k: {})
    monkeypatch.setattr(conflicts, "same_key_mark", AsyncMock(return_value=None))
    releasing, finish = asyncio.Event(), asyncio.Event()
    release = leases.release_shielded

    class CH:
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

        async def insert(self, table, batch, **kw):
            assert table == "loom.memory"

    async def held_release(lease):
        releasing.set()
        await finish.wait()
        await release(lease)

    monkeypatch.setattr(leases, "release_shielded", held_release)
    ch = CH()
    if operation == "plain_set":
        call = set_commit._land_plain_row(w, ch, high=False)
    elif operation == "searchable_set":
        call = set_commit._set_dual_write(
            w, ch, SimpleNamespace(policy={}), SimpleNamespace(verdict="clear")
        )
    else:
        call = update._land_inserted(
            source, memory, [], ch, retire_vectors=False, high=False, record=revision
        )
    task = asyncio.create_task(call)
    await releasing.wait()
    task.cancel("release-stage")
    finish.set()
    await following.wait()
    if cancel_again:
        task.cancel("follow-up-stage")
    finish_following.set()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    assert cancelled.value.args == ("release-stage",)
    if operation == "update":
        revision.assert_awaited_once()
        assert completed == ["revision"]
    else:
        receipt.assert_awaited_once()
        announce.assert_awaited_once_with(w)
        assert set(completed) == {"receipt", "announce"}


@pytest.mark.asyncio
async def test_unacknowledged_restore_leaves_its_mirror_for_retry(monkeypatch):
    from unittest.mock import Mock

    from server.core.errors import BackendUnavailableError
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import restore
    from server.memory.store.rows import _hydrate

    source = _row()
    restored = store.build_memory_version_row(source, version=200)
    rows = []

    class LostAnswer:
        search_deleted = True

        async def insert(self, table, batch, **kw):
            rows.extend(batch)
            raise RuntimeError("the server's insert answer was lost")

        async def query_all(self, sql, params):
            if "FROM loom.memory_refusals" in sql:
                return []
            if "FROM loom.memory_refusals" in sql:
                return []
            if "PREWHERE (org, namespace" in sql:
                return [source.model_dump()]
            assert "AS search_deleted" in sql
            return [{"history": [(200, False), (100, True)], "search_deleted": self.search_deleted}]

    ch = LostAnswer()
    mirror_calls = []

    async def mirror(*args, **kw):
        mirror_calls.append(True)
        ch.search_deleted = False

    committed, announced = Mock(), AsyncMock()
    monkeypatch.setattr(restore, "_restore_landed", AsyncMock(return_value=None))
    monkeypatch.setattr(restore, "_read_restorable", AsyncMock(return_value=_hydrate(restored)))
    monkeypatch.setattr(restore, "_restore_embedding_mirror", mirror)
    monkeypatch.setattr(restore.blacklist_guard, "enforce_rows", AsyncMock(return_value={}))
    request = MemoryRestoreInput(
        org=source.org,
        namespace=source.namespace,
        agent=source.agent,
        memory_key=source.memory_key,
    )
    with pytest.raises(BackendUnavailableError) as unknown:
        await restore._insert_restore(
            request,
            restored,
            source,
            ch=ch,
            what="the restore",
            mirror=mirror,
            landed=announced,
            on_commit=committed,
            policy={},
        )
    assert unknown.value.extra["outcome_unknown"]
    assert rows == [restored]
    committed.assert_not_called()
    announced.assert_not_awaited()
    assert mirror_calls == []
    retry = await restore._restore_live(request, restored, ch=ch)
    assert retry.restored and retry.from_status == "tombstoned"
    assert mirror_calls == [True]
    assert not (await restore._restore_live(request, restored, ch=ch)).restored


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["landed", "then"])
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_acknowledged_copy_finishes_followups_under_repeated_cancellation(
    monkeypatch, stage, cancel_again
):
    import asyncio

    from server.memory.store import lifecycle

    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []
    original = asyncio.CancelledError("acknowledged holder cleanup cancelled")
    insert = lifecycle.insert_memory_versions

    class CH:
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

        async def insert(self, *args, **kw):
            return None

    async def inserted_then_cancelled(*args, **kw):
        await insert(*args, **kw)
        # The funnel has acknowledged the row before cleanup cancellation surfaces.
        raise original

    async def landed():
        calls.append("landed")
        if stage == "landed":
            entered.set()
            await finish.wait()
        calls.append("landed_done")

    async def then():
        calls.append("then")
        if stage == "then":
            entered.set()
            await finish.wait()
        calls.append("then_done")
        return 1

    monkeypatch.setattr(lifecycle, "insert_memory_versions", inserted_then_cancelled)
    task = asyncio.create_task(
        store.insert_versions_over_heads(
            [store.build_memory_version_row(_row())], [_row()], ch=CH(), landed=landed, then=then
        )
    )
    try:
        await entered.wait()
        if cancel_again:
            task.cancel("cancel again during follow-up")
        finish.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value is original
        assert calls == ["landed", "landed_done", "then", "then_done"]
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_bulk_embedding_failure_still_announces_acknowledged_memory(monkeypatch, failure):
    original = failure("embedding write interrupted")
    announced = AsyncMock()
    memory = store.build_memory_version_row(_row())
    inserted = []

    class CH:
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

        async def insert(self, table, rows, **kw):
            assert table == "loom.memory"
            inserted.extend(rows)

    monkeypatch.setattr(store, "insert_embedding_rows", AsyncMock(side_effect=original))
    monkeypatch.setattr(bulk_set, "_bulk_announce", announced)
    with pytest.raises(failure) as caught:
        await bulk_set._land_batch(
            CH(), SimpleNamespace(memory_rows=[memory], embedding_rows=[]), None, {}
        )
    assert caught.value is original
    assert inserted == [memory]
    announced.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_bulk_followup_finishes_under_repeated_cancellation(monkeypatch, cancel_again):
    from server.core import leases

    releasing, finish_release = asyncio.Event(), asyncio.Event()
    announcing, finish_announce = asyncio.Event(), asyncio.Event()
    release = leases.release_shielded
    completed = []

    class CH:
        async def query_all(self, *args, **kwargs):
            if "FROM loom.memory_refusals" in args[0]:
                return []
            return [_row().model_dump()]

        async def insert(self, *args, **kw):
            return None

    async def held_release(lease):
        releasing.set()
        await finish_release.wait()
        await release(lease)

    async def announce(state):
        announcing.set()
        await finish_announce.wait()
        completed.append("announced")

    monkeypatch.setattr(leases, "release_shielded", held_release)
    monkeypatch.setattr(bulk_set, "_bulk_announce", announce)
    task = asyncio.create_task(
        bulk_set._land_batch(
            CH(),
            SimpleNamespace(
                memory_rows=[store.build_memory_version_row(_row())], embedding_rows=[]
            ),
            None,
            {},
        )
    )
    await releasing.wait()
    task.cancel("release-stage")
    finish_release.set()
    await announcing.wait()
    if cancel_again:
        task.cancel("follow-up-stage")
    finish_announce.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value.args == ("release-stage",)
    assert completed == ["announced"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_again", [False, True])
async def test_high_fence_finishes_all_stores_under_repeated_cancellation(
    monkeypatch, cancel_again
):
    from server.memory.app import high_fence

    entered, finish = asyncio.Event(), asyncio.Event()
    original = asyncio.CancelledError("memory send interrupted")
    completed = []

    async def vectors(*args, **kw):
        entered.set()
        await finish.wait()
        completed.append("vectors")

    async def chunks(*args, **kw):
        completed.append("chunks")

    async def recheck(*args, **kw):
        completed.append("recheck")

    async def write():
        try:
            raise original
        finally:
            await high_fence.fence_high([(identity_key(_row()), _row().version)])

    monkeypatch.setattr(high_fence, "get_embedder", lambda: SimpleNamespace(model_name="m"))
    monkeypatch.setattr(store, "fence_vectors", vectors)
    monkeypatch.setattr(high_fence, "tombstone_chunks_of", chunks)
    monkeypatch.setattr(high_fence, "recheck_live_vectors", recheck)
    task = asyncio.create_task(write())
    await entered.wait()
    if cancel_again:
        task.cancel("fence-stage")
    finish.set()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert caught.value is original
    assert completed == ["vectors", "chunks", "recheck"]
