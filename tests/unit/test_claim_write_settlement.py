"""A cleanup claimant cannot pass a sent INSERT that may still append raw metadata."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import ch as ch_module
from server.core import leases, rmt_carry
from server.settings import settings
from tests.unit.clock_fake import ManualClock

pytestmark = pytest.mark.asyncio


class Keeper:
    """External claim acquisition/release and server-side expiry, not the claim protocol."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.clock = ManualClock(1000.0)
        self.keys: dict[str, tuple[leases.Lease, float]] = {}
        self.serial = 0
        monkeypatch.setattr(leases, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        monkeypatch.setattr(rmt_carry, "time", SimpleNamespace(monotonic=self.clock.monotonic))
        monkeypatch.setattr(settings, "ch_timeout_write_s", 5)
        monkeypatch.setattr(leases, "take", self.take)

    async def take(self, name: str, *, ttl_s: float, wait_s: float, ch: Any):
        assert wait_s >= 0
        if name in self.keys and self.keys[name][1] >= self.clock():
            return None
        self.serial += 1
        lease = leases.Lease(name, ttl_s=ttl_s, ch=self, holder=str(self.serial))
        lease._renewed = self.clock()
        lease._sent = True
        self.keys[name] = lease, self.clock() + ttl_s
        return lease

    async def execute(self, sql: str, *, params: dict[str, Any]) -> None:
        assert sql == leases.LOCK_RELEASE
        name = params["name"]
        current = self.keys.get(name)
        if current is not None and current[0].holder == params["me"]:
            del self.keys[name]

    def send(self, name: str) -> float:
        deadline = rmt_carry.clock_send_deadline((self.clock(), self.clock()))
        rmt_carry.note_send(name, deadline, ch_module.write_limit_s())
        return deadline


async def test_acknowledged_sends_release_without_waiting(monkeypatch):
    keeper = Keeper(monkeypatch)
    async with leases.holding("claim", ttl_s=60, settle_writes=True) as held:
        assert held
        keeper.send("write")
        rmt_carry.note_ended("write")  # A server answer, including its own error, ended it.
    assert keeper.keys == {} and keeper.clock.sleeps == []


async def test_cancelled_unanswered_send_keeps_keeper_key_until_its_safe_expiry(monkeypatch):
    keeper = Keeper(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        async with leases.holding("claim", ttl_s=60, settle_writes=True):
            deadline = keeper.send("write")
            raise asyncio.CancelledError
    async with leases.holding("claim", ttl_s=60, settle_writes=True) as held:
        assert not held
    assert deadline + rmt_carry.send_bound_s() - 15 <= keeper.keys["claim"][1]
    keeper.clock.advance(61)
    async with leases.holding("claim", ttl_s=60, settle_writes=True) as held:
        assert held
    assert keeper.keys == {} and keeper.clock.sleeps == []


async def test_handled_lost_reply_cannot_turn_an_unsettled_claim_into_success(monkeypatch):
    keeper = Keeper(monkeypatch)
    with pytest.raises(leases.LeaseUnavailable, match="unanswered"):
        async with leases.holding("claim", ttl_s=60, settle_writes=True):
            keeper.send("lost-reply")
    assert "claim" in keeper.keys


async def test_nested_claim_cannot_move_outer_send_deadline_or_release_its_unknown_send(
    monkeypatch,
):
    keeper = Keeper(monkeypatch)
    with pytest.raises(asyncio.CancelledError):
        async with leases.holding("outer", ttl_s=60, settle_writes=True):
            outer = rmt_carry.clock_send_deadline((keeper.clock(), keeper.clock()))
            keeper.clock.advance(20)
            async with leases.holding("inner", ttl_s=60, settle_writes=True):
                inner = keeper.send("late-write")
                assert inner > outer
                assert inner + 5 + 5 <= keeper.keys["outer"][1]
                raise asyncio.CancelledError
    assert set(keeper.keys) == {"outer", "inner"}
    keeper.clock.advance(41)
    async with leases.holding("outer", ttl_s=60, settle_writes=True) as held:
        assert held
    async with leases.holding("inner", ttl_s=60, settle_writes=True) as held:
        assert not held


async def test_impossible_receiver_bound_refuses_before_body_without_retaining_empty_claim(
    monkeypatch,
):
    keeper = Keeper(monkeypatch)
    with pytest.raises(leases.ClaimExpired):
        async with leases.holding("claim", ttl_s=10, settle_writes=True):
            pytest.fail("no work fits before the receiver's completion barrier")
    assert keeper.keys == {}


async def test_default_write_timeout_still_leaves_a_full_claim_work_window(monkeypatch):
    keeper = Keeper(monkeypatch)
    monkeypatch.setattr(settings, "ch_timeout_write_s", 60)
    assert ch_module.write_limit_s() == 60 and rmt_carry.send_bound_s() == 80
    async with leases.holding("claim", ttl_s=60, settle_writes=True) as held:
        assert held and ch_module.write_limit_s() == 9
        assert rmt_carry.send_bound_s() == 29
        keeper.clock.advance(23)
        deadline = keeper.send("near-work-end")
        assert deadline + ch_module.write_limit_s() + 5 < keeper.keys["claim"][1]
        rmt_carry.note_ended("near-work-end")
    assert keeper.keys == {} and keeper.clock.sleeps == []
