"""Registered deferred-work drains are positive, bounded and non-cancelling."""

import asyncio
import gc
import weakref
from types import SimpleNamespace

import pytest

from server.core import background
from server.memory.app import after_write, conflict_deferred


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(background, "_registered_tasks", set())


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduler", ["after_write", "conflict_deferred"])
async def test_memory_deferred_writers_are_visible_to_model_drain(monkeypatch, scheduler):
    release = asyncio.Event()

    async def write(*args, **kwargs):
        await release.wait()
        return "clear"

    if scheduler == "after_write":
        task = after_write.schedule(write(), name="test-memory-after-write")
    else:
        monkeypatch.setattr(conflict_deferred, "settle", write)
        monkeypatch.setattr(conflict_deferred, "_PENDING", {})
        result = SimpleNamespace(pending_judge=object(), verdict="pending")
        task = conflict_deferred.schedule_settle(None, result, memory_id="test-drain")
    assert task is not None
    try:
        assert not await background.drain_task_sets(timeout_s=0)
        assert not task.done()
    finally:
        release.set()
        await task
    assert await background.drain_task_sets(timeout_s=0)


@pytest.mark.asyncio
async def test_empty_registry_is_quiescent():
    assert await background.drain_task_sets(timeout_s=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduler", ["read_heal", "write_heal"])
async def test_profile_reembedding_is_visible_after_request_returns(monkeypatch, scheduler):
    from server.memory.app import profile_search

    release = asyncio.Event()

    async def write(*args, **kwargs):
        await release.wait()

    monkeypatch.setattr(profile_search, "queue_profile_reembed", write)
    monkeypatch.setattr(profile_search, "reembed_after_write", write)
    if scheduler == "read_heal":
        profile_search._queue_detached("org", [], None)
    else:
        profile_search.schedule_reembed_after_write("org", ("agent", "-", "-", "key"))
    try:
        assert not await background.drain_task_sets(timeout_s=0)
    finally:
        release.set()
        assert await background.drain_task_sets(timeout_s=1)


@pytest.mark.asyncio
async def test_timeout_keeps_pending_work_alive():
    tasks = background.TaskSet()
    release = asyncio.Event()
    task = tasks.spawn(release.wait(), name="deferred-test")
    assert not await background.drain_task_sets(timeout_s=0.001)
    assert not task.done()
    release.set()
    assert await background.drain_task_sets(timeout_s=1)
    assert task.done() and not task.cancelled()


@pytest.mark.asyncio
async def test_cancelling_the_drain_does_not_cancel_writes():
    tasks = background.TaskSet()
    release = asyncio.Event()
    write = tasks.spawn(release.wait(), name="deferred-test")
    drain = asyncio.create_task(background.drain_task_sets(timeout_s=10))
    await asyncio.sleep(0)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    assert not write.done()
    release.set()
    await write


@pytest.mark.asyncio
async def test_transitive_work_in_a_new_set_must_finish_too():
    tasks = background.TaskSet()
    release = asyncio.Event()
    children = []

    async def parent():
        child_set = background.TaskSet()
        children.append(child_set.spawn(release.wait(), name="child-write"))

    task = tasks.spawn(parent(), name="parent-write")
    await task
    assert not await background.drain_task_sets(timeout_s=0.001)
    release.set()
    assert await background.drain_task_sets(timeout_s=1)
    assert children[0].done()


@pytest.mark.asyncio
async def test_completed_parent_with_queued_callback_is_not_quiescent():
    tasks = background.TaskSet()
    release = asyncio.Event()
    children = []

    async def parent():
        pass

    task = tasks.spawn(parent(), name="completed-parent")
    await task
    task.add_done_callback(
        lambda _: children.append(tasks.spawn(release.wait(), name="callback-child"))
    )
    try:
        assert not await background.drain_task_sets(timeout_s=0)
    finally:
        release.set()
        await asyncio.sleep(0)
        await asyncio.gather(*children)


@pytest.mark.asyncio
async def test_tracked_coordinator_cannot_acknowledge_its_own_drain():
    tasks = background.TaskSet()
    task = tasks.spawn(background.drain_task_sets(timeout_s=1), name="wrong-coordinator")
    with pytest.raises(RuntimeError, match="tracked task"):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [-1, float("nan"), float("inf")])
async def test_invalid_timeout_never_claims_quiescence(invalid_value):
    with pytest.raises(ValueError, match="finite and nonnegative"):
        await background.drain_task_sets(timeout_s=invalid_value)


def test_registry_does_not_retain_unused_task_sets():
    tasks = background.TaskSet()
    reference = weakref.ref(tasks)
    assert not background._registered_tasks
    del tasks
    gc.collect()
    assert reference() is None
    assert not background._registered_tasks
