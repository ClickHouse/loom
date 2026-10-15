"""A cancelled request cannot race Keeper acquisition with connection cleanup."""

import asyncio
import threading
from unittest.mock import Mock

import pytest

from server.insights.store import provider_lock


@pytest.mark.asyncio
async def test_cancelled_acquisition_finishes_before_release(monkeypatch: pytest.MonkeyPatch):
    started = asyncio.Event()
    loop = asyncio.get_running_loop()
    proceed = threading.Event()
    events: list[str] = []

    def acquire():
        loop.call_soon_threadsafe(started.set)
        assert proceed.wait(5)
        events.append("acquired")
        return "held"

    client = Mock()
    client.close.side_effect = lambda: events.append("closed")
    lock = Mock()
    lock.acquire.side_effect = acquire
    lock.release.side_effect = lambda: events.append("released")
    monkeypatch.setattr(provider_lock, "client", lambda: client)
    monkeypatch.setattr(provider_lock, "ProcessKeeperLock", lambda *args, **kwargs: lock)

    async def save():
        async with provider_lock.configuration_lock():
            events.append("entered")

    task = asyncio.create_task(save())
    try:
        async with asyncio.timeout(5):
            await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert events == []
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert events == ["acquired", "released", "closed"]
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_unavailable_lock_closes_client(monkeypatch: pytest.MonkeyPatch):
    client = Mock()
    lock = Mock()
    lock.acquire.return_value = "unleased"
    monkeypatch.setattr(provider_lock, "client", lambda: client)
    monkeypatch.setattr(provider_lock, "ProcessKeeperLock", lambda *args, **kwargs: lock)
    with pytest.raises(provider_lock.ConfigurationLockUnavailable):
        async with provider_lock.configuration_lock():
            pytest.fail("must not save without the shared lock")
    lock.release.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_connection_is_closed_when_connect_returns(monkeypatch: pytest.MonkeyPatch):
    started = asyncio.Event()
    proceed = threading.Event()
    loop = asyncio.get_running_loop()
    client = Mock()

    def connect():
        loop.call_soon_threadsafe(started.set)
        assert proceed.wait(5)
        return client

    monkeypatch.setattr(provider_lock, "client", connect)

    async def save():
        async with provider_lock.configuration_lock():
            pytest.fail("cancelled connection must not enter critical section")

    task = asyncio.create_task(save())
    try:
        async with asyncio.timeout(5):
            await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        client.close.assert_not_called()
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        client.close.assert_called_once()
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_a_caller_sets_the_wait_and_hears_who_holds_the_lock(monkeypatch: pytest.MonkeyPatch):
    seen = {}
    lock = Mock()
    lock.acquire.return_value = "held"

    def keeper_lock(*args, **kwargs):
        seen.update(kwargs)
        return lock

    monkeypatch.setattr(provider_lock, "client", Mock)
    monkeypatch.setattr(provider_lock, "ProcessKeeperLock", keeper_lock)
    heard: list[str] = []
    async with provider_lock.configuration_lock(wait_s=300, out=heard.append):
        pass
    assert seen["cfg"].wait_s == 300 and seen["cfg"].ttl_s == 300
    seen["out"]("held by another")
    assert heard == ["held by another"]
