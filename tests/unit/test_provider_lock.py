"""The provider configuration lock: held for the block, given up after it, and a
block that loses it is cancelled rather than writing on."""

import asyncio

import pytest

from server.core import leases
from server.insights.store import provider_lock
from tests.unit.claims_fake import FakeLockTable


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> FakeLockTable:
    fake = FakeLockTable()
    monkeypatch.setattr("server.core.ch.get_client", lambda: fake)
    return fake


async def test_the_lock_is_held_for_the_block_and_released(ch):
    async with provider_lock.configuration_lock() as lock:
        assert lock.held and ch.holder(provider_lock.LOCK_NAME) == lock.holder
    assert ch.keys == {}


async def test_a_held_lock_is_unavailable_after_the_wait(ch):
    ch.seed(provider_lock.LOCK_NAME, "other/1")
    with pytest.raises(provider_lock.ConfigurationLockUnavailable):
        async with provider_lock.configuration_lock(wait_s=0.2):
            pytest.fail("must not save without the shared lock")


async def test_a_lock_store_that_cannot_answer_is_unavailable(ch):
    ch.down = True
    with pytest.raises(provider_lock.ConfigurationLockUnavailable):
        async with provider_lock.configuration_lock():
            pytest.fail("must not save without the shared lock")


async def test_a_lost_lock_cancels_the_block(ch, monkeypatch):
    monkeypatch.setattr(provider_lock, "LOCK_TTL_S", 0.6)
    wrote = False
    with pytest.raises(leases.LeaseLost):
        async with provider_lock.configuration_lock():
            ch.keys[provider_lock.LOCK_NAME]["holder"] = "other/1"
            await asyncio.sleep(2)
            wrote = True
    assert not wrote
