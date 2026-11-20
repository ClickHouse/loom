"""An uncertain send keeps its durable fence until its server deadline passes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.core import lease_writes as writes


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, TimeoutError, asyncio.CancelledError])
async def test_fence_retains_only_uncertain_holder(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException] | None
) -> None:
    state = SimpleNamespace(expired=False, deadline=0.0)

    def expire() -> None:
        state.expired = True

    lease = SimpleNamespace(let_expire=expire)
    take = AsyncMock(return_value=lease)
    release = AsyncMock()
    monkeypatch.setattr(writes.leases, "take", take)
    monkeypatch.setattr(writes.leases, "release_shielded", release)
    monkeypatch.setattr(writes.leases, "work_left", lambda *args: 24.0)
    monkeypatch.setattr(writes.rmt_carry, "send_bound_s", lambda: 30)

    @contextmanager
    def deadline(value: float):
        state.deadline = value
        yield

    monkeypatch.setattr(writes.rmt_carry, "under_lease", deadline)

    async def work() -> None:
        async with writes.fenced("scope") as uncertain:
            uncertain(True)
            if failure:
                raise failure()
            uncertain(False)

    if failure:
        with pytest.raises(failure):
            await work()
    else:
        await work()
    assert state.expired == bool(failure)
    assert state.deadline > writes.time.monotonic()
    ttl = take.call_args.kwargs["ttl_s"]
    assert 0.4 * ttl + 30 + 5 <= ttl
    release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_unavailable_fence_never_enters_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(writes.leases, "take", AsyncMock(return_value=None))
    with pytest.raises(writes.BackendUnavailableError):
        async with writes.fenced("scope"):
            pytest.fail("writer admitted without fence")


def test_nested_fences_keep_the_earliest_send_deadline() -> None:
    from server.core import rmt_carry

    with rmt_carry.under_lease(10):
        with rmt_carry.under_lease(20):
            assert rmt_carry._lease_end.get() == 10
        with rmt_carry.under_lease(5):
            assert rmt_carry._lease_end.get() == 5
        assert rmt_carry._lease_end.get() == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_batch_admission_failure_releases_successful_peers(monkeypatch, cancel):
    entered = asyncio.Event()
    lease = SimpleNamespace(let_expire=lambda: None)
    released = AsyncMock()

    async def take(name, **kw):
        if name == "a":
            return lease
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("control store failed")

    monkeypatch.setattr(writes.leases, "take", take)
    monkeypatch.setattr(writes.leases, "release_shielded", released)

    async def write():
        async with writes.fenced_many(["a", "b"]):
            pytest.fail("a partial admission cannot write")

    task = asyncio.create_task(write())
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    released.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_batch_checks_the_earliest_holder_before_sending(monkeypatch):
    first, second = SimpleNamespace(), SimpleNamespace()
    take = AsyncMock(side_effect=[first, second])
    release = AsyncMock()
    monkeypatch.setattr(writes.leases, "take", take)
    monkeypatch.setattr(writes.leases, "release_shielded", release)
    monkeypatch.setattr(writes.leases, "work_left", lambda lease, ttl: 0 if lease is first else 24)
    with pytest.raises(writes.FenceUnavailableError, match="expired"):
        async with writes.fenced_many(["a", "b"]):
            pytest.fail("batch admission exhausted its earliest holder")
    assert release.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", [0, 1, 8, 64])
async def test_batch_release_does_not_overload_its_own_pool(monkeypatch, pool):
    names = [f"k{i}" for i in range(1000)]
    remaining = set(names)
    active = 0
    peak = 0

    async def take(name, **kw):
        return SimpleNamespace(name=name)

    async def release(lease):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if active > (pool or 64):
                return  # a refused release leaves its durable holder in place
            await asyncio.sleep(0)
            remaining.remove(lease.name)
        finally:
            active -= 1

    monkeypatch.setattr(writes.leases, "take", take)
    monkeypatch.setattr(writes.settings, "ch_pool_maxsize", pool)
    monkeypatch.setattr(writes.settings, "ch_write_fence_concurrency", 3)
    monkeypatch.setattr(writes.leases, "work_left", lambda *args: 24)
    monkeypatch.setattr(writes.leases, "release_shielded", release)
    async with writes.fenced_many(names):
        pass
    assert not remaining, "cleanup cannot strand keys by exceeding the connection pool"
    assert peak == min(3, pool or 3)


@pytest.mark.asyncio
async def test_waiting_batch_releases_partial_admission_before_retry(monkeypatch):
    held = set()
    attempts = iter(["a", None, "a", "b"])
    elapsed = 0.0

    async def take(name, **kw):
        result = next(attempts)
        if result is None:
            return None
        held.add(result)
        return SimpleNamespace(name=result)

    async def release(lease):
        held.remove(lease.name)

    async def pause(delay):
        nonlocal elapsed
        assert held == set()
        elapsed += delay

    monkeypatch.setattr(writes.leases, "take", take)
    monkeypatch.setattr(writes.leases, "release_shielded", release)
    monkeypatch.setattr(writes.leases, "work_left", lambda *args: 24)
    monkeypatch.setattr(writes, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(writes.asyncio, "sleep", pause)
    async with writes.fenced_many(["a", "b"], wait_s=1):
        assert held == {"a", "b"}
    assert not held


@pytest.mark.asyncio
async def test_admission_wait_stops_at_its_deadline(monkeypatch):
    elapsed = 0.0

    async def pause(delay):
        nonlocal elapsed
        elapsed += delay

    monkeypatch.setattr(writes.leases, "take", AsyncMock(return_value=None))
    monkeypatch.setattr(writes, "time", SimpleNamespace(monotonic=lambda: elapsed))
    monkeypatch.setattr(writes.asyncio, "sleep", pause)
    with pytest.raises(writes.FenceUnavailableError):
        async with writes.fenced_many(["busy"], wait_s=0.25):
            pytest.fail("a busy holder was admitted")
    assert elapsed == pytest.approx(0.25)


@pytest.mark.asyncio
@pytest.mark.parametrize("sending", [False, True])
async def test_fence_deadline_is_typed_and_preserves_unknown_outcome(monkeypatch, sending):
    from unittest.mock import Mock

    from server.core.errors import BackendUnavailableError

    lease = SimpleNamespace(let_expire=Mock())
    release = AsyncMock()
    monkeypatch.setattr(writes.leases, "take", AsyncMock(return_value=lease))
    monkeypatch.setattr(writes.leases, "release_shielded", release)
    monkeypatch.setattr(writes.leases, "work_left", lambda *args: 0.001)
    with pytest.raises(BackendUnavailableError) as failed:
        async with writes.fenced_many(["key"]) as uncertain:
            uncertain(sending)
            await asyncio.Event().wait()
    assert failed.value.status_code == 503
    assert failed.value.extra["outcome_unknown"] is sending
    assert failed.value.extra["retry_after_s"] > 0
    assert lease.let_expire.call_count == int(sending)
    release.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_inner_timeout_keeps_its_original_error(monkeypatch):
    from unittest.mock import Mock

    lease = SimpleNamespace(let_expire=Mock())
    release = AsyncMock()
    monkeypatch.setattr(writes.leases, "take", AsyncMock(return_value=lease))
    monkeypatch.setattr(writes.leases, "release_shielded", release)
    monkeypatch.setattr(writes.leases, "work_left", lambda *args: 24)
    inner = TimeoutError("client insert outcome unknown")
    with pytest.raises(TimeoutError) as failed:
        async with writes.fenced_many(["key"]) as uncertain:
            uncertain(True)
            raise inner
    assert failed.value is inner
    lease.let_expire.assert_called_once()
    release.assert_awaited_once_with(lease)
