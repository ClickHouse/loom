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
