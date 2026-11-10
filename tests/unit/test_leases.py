"""Deployment-wide leases (`server.core.leases`) against a fake of the Keeper lock
table: the key, the wait, renewal and loss."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core import leases
from server.core.errors import BackendUnavailableError
from tests.unit.claims_fake import FakeLockTable


def _inserts(ch: FakeLockTable) -> list[str]:
    return [s for s in ch.sql if s.startswith("INSERT")]


async def test_a_lease_is_one_key_taken_and_released() -> None:
    ch = FakeLockTable()
    lease = await leases.take("job", ttl_s=60, ch=ch)
    assert lease is not None and lease.held and ch.holder("job") == lease.holder
    await lease.release()
    assert ch.keys == {}


async def test_a_lease_left_to_expire_keeps_its_key_after_its_block() -> None:
    """A holder that cannot tell whether its last write lands stops renewing
    and leaves the key to its TTL: leaving the block deletes nothing."""
    ch = FakeLockTable()
    lease = await leases.take("job", ttl_s=60, ch=ch)
    assert lease is not None
    async with lease.renewed():
        lease.let_expire()
    assert ch.holder("job") == lease.holder


async def test_a_held_key_is_read_not_inserted_against() -> None:
    """An INSERT of a held key is a KEEPER_EXCEPTION in the server's log, every poll."""
    ch = FakeLockTable()
    ch.seed("job", "other")
    assert await leases.take("job", ttl_s=60, wait_s=0.3, poll_s=0.05, ch=ch) is None
    assert _inserts(ch) == [] and len(ch.sql) > 2
    assert ch.holder("job") == "other"


async def test_a_name_expected_free_is_inserted_without_a_read() -> None:
    ch = FakeLockTable()
    lease = await leases.take("idem:k", ttl_s=60, ch=ch, expect_free=True)
    assert lease is not None
    assert [s for s in ch.sql if "FROM loom._migration_lock" in s] == []
    ch.sql.clear()
    assert await leases.take("idem:k", ttl_s=60, ch=ch, expect_free=True) is None
    assert ch.sql[0].startswith("INSERT") and "FROM loom._migration_lock" in ch.sql[1]


async def test_a_stale_key_that_names_me_is_renewed_before_it_counts_as_mine() -> None:
    """Another process could take over a stale key at once: it is renewed back first."""
    ch = FakeLockTable()
    ch.seed("job", "me", stale=True)
    lease = await leases.take("job", ttl_s=60, ch=ch, holder="me")
    assert lease is not None
    assert any(" UPDATE renewed_at = " in s for s in ch.sql)
    assert not ch._stale(ch.keys["job"])


async def test_holding_yields_whether_the_claim_is_mine_and_releases_it() -> None:
    ch = FakeLockTable()
    async with leases.holding("x", ttl_s=5, ch=ch) as held:
        assert held and ch.holder("x") is not None
        async with leases.holding("x", ttl_s=5, ch=ch) as again:
            assert not again
    assert ch.keys == {}


async def test_an_expired_key_is_taken_over_unless_its_holder_may_still_run() -> None:
    ch = FakeLockTable()
    ch.seed("job", "crashed", stale=True)

    async def still_running(holder: str) -> bool:
        return False

    assert await leases.take("job", ttl_s=60, ch=ch, may_take_over=still_running) is None
    lease = await leases.take("job", ttl_s=60, ch=ch)
    assert lease is not None and ch.holder("job") == lease.holder


async def test_a_wait_takes_the_lease_once_its_holder_releases() -> None:
    ch = FakeLockTable()
    ch.seed("job", "other")
    asyncio.get_running_loop().call_later(0.2, ch.keys.clear)
    lease = await leases.take("job", ttl_s=60, wait_s=3, poll_s=0.05, ch=ch)
    assert lease is not None and ch.holder("job") == lease.holder


async def test_no_attempt_starts_after_the_wait() -> None:
    ch = FakeLockTable()
    ch.seed("job", "other")
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await leases.take("job", ttl_s=60, wait_s=0.5, poll_s=0.2, ch=ch) is None
    assert loop.time() - started < 0.8
    reads = len(ch.sql)
    await asyncio.sleep(0.3)
    assert len(ch.sql) == reads


async def test_a_take_cancelled_after_its_insert_landed_releases_the_key() -> None:
    class SlowReply(FakeLockTable):
        def __init__(self) -> None:
            super().__init__()
            self.landed = asyncio.Event()

        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            await super().execute(sql, params, **kw)
            if sql.startswith("INSERT"):
                self.landed.set()
                await asyncio.sleep(5)

    ch = SlowReply()
    taking = asyncio.create_task(leases.take("job", ttl_s=60, ch=ch))
    await ch.landed.wait()
    assert ch.keys
    taking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await taking
    assert ch.keys == {}


async def test_a_store_that_cannot_answer_is_unavailable_not_busy() -> None:
    ch = FakeLockTable()
    ch.down = True
    with pytest.raises(leases.LeaseUnavailable):
        await leases.take("job", ttl_s=60, ch=ch)
    assert issubclass(leases.LeaseUnavailable, BackendUnavailableError)
    assert not issubclass(leases.LeaseUnavailable, leases.LeaseBusy)


async def test_a_lease_is_renewed_while_its_block_runs() -> None:
    ch = FakeLockTable()
    async with leases.cluster_lease("job", ttl_s=0.6, ch=ch) as lease:
        ch.advance(0.4)  # the server's clock: past the TTL of the first stamp
        await asyncio.sleep(0.5)
        assert lease.held and not ch._stale(ch.keys["job"])
    assert ch.keys == {}


async def test_a_lease_taken_over_cancels_its_block() -> None:
    ch = FakeLockTable()
    ran_on = False
    with pytest.raises(leases.LeaseLost):
        async with leases.cluster_lease("job", ttl_s=0.6, ch=ch):
            ch.keys["job"]["holder"] = "other"
            await asyncio.sleep(2)
            ran_on = True
    assert not ran_on and ch.holder("job") == "other"


async def test_a_lease_whose_renewals_fail_is_lost_at_half_its_ttl() -> None:
    ch = FakeLockTable()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(leases.LeaseLost):
        async with leases.cluster_lease("job", ttl_s=0.6, ch=ch) as lease:
            ch.down = True
            await asyncio.sleep(2)
    assert 0.25 <= loop.time() - started < 1.0 and not lease.held  # long before the 2 s block


async def test_a_renewal_that_recovers_before_half_the_ttl_keeps_the_lease() -> None:
    ch = FakeLockTable()
    async with leases.cluster_lease("job", ttl_s=1.2, ch=ch) as lease:
        ch.down = True  # the renewal at a sixth of the TTL fails
        await asyncio.sleep(0.25)
        ch.down = False  # the retry a twelfth later lands
        await asyncio.sleep(1.0)
        assert lease.held


async def test_a_pause_past_half_the_ttl_counts_as_lost_without_a_call() -> None:
    ch = FakeLockTable()
    lease = await leases.take("job", ttl_s=60, ch=ch)
    assert lease is not None and lease.held
    lease._renewed -= 31  # stopped (SIGSTOP, a VM pause) past the bound
    assert not lease.held


async def test_a_renewed_claim_outlives_its_ttl_until_released() -> None:
    ch = FakeLockTable()
    lease = await leases.take("idem:k", ttl_s=0.6, renew=True, ch=ch)
    assert lease is not None
    ch.advance(0.4)
    await asyncio.sleep(0.8)
    assert lease.held and not ch._stale(ch.keys["idem:k"])
    await lease.release()
    assert ch.keys == {}


async def test_the_sweep_deletes_only_keys_long_past_their_expiry() -> None:
    ch = FakeLockTable()
    ch.seed("live", "a")
    ch.seed("dead", "b", ttl=60)
    ch.keys["dead"]["renewed"] -= 4000
    assert await leases.sweep_dead_keys(ch) == 2
    assert list(ch.keys) == ["live"]


async def test_the_keeper_sweep_worker_sweeps_on_the_leader(monkeypatch) -> None:
    from server.workers import keeper_sweep, registry

    ch = FakeLockTable()
    ch.seed("dead", "b")
    ch.keys["dead"]["renewed"] -= 4000
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    assert await keeper_sweep.run_pass() == 2 and ch.keys == {}
    assert "keeper_sweep" in {w.name for w in registry._WORKERS}


async def test_a_claim_whose_reply_came_after_its_ttl_never_opens_its_block() -> None:
    """The insert landed but its reply was held past the TTL; a successor took the
    expired key over and is inside its block. The late claimant's budget ran from
    when its attempt was sent, so it gets none: one block runs, not two."""

    class LateReply(FakeLockTable):
        first = True

        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            await super().execute(sql, params, **kw)
            if sql.startswith("INSERT") and self.first:
                self.first = False
                await asyncio.sleep(0.6)

    ch = LateReply()
    entered: list[str] = []

    async def claim(who: str, wait_s: float) -> None:
        async with leases.holding("signup:x", ttl_s=0.5, wait_s=wait_s, ch=ch) as held:
            assert held
            entered.append(who)
            await asyncio.sleep(0.05)

    late = asyncio.create_task(claim("late", 0))
    await asyncio.sleep(0.05)
    successor = asyncio.create_task(claim("successor", 2))
    with pytest.raises(leases.ClaimExpired):
        await late
    await successor
    assert entered == ["successor"]


async def test_one_renewal_that_hangs_is_retried_before_the_lease_counts_as_lost() -> None:
    """A renewal whose ClickHouse call hangs is cut short and retried, so one hung call
    well inside the window does not lose the lease."""

    class HangsOnce(FakeLockTable):
        def __init__(self) -> None:
            super().__init__()
            self.renewals = 0

        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            if sql == leases.LOCK_RENEW:
                self.renewals += 1
                if self.renewals == 1:
                    await asyncio.sleep(10)  # the first renewal's call never answers in time
            await super().execute(sql, params, **kw)

    ch = HangsOnce()
    lease = await leases.take("job", ttl_s=1.2, ch=ch)
    assert lease is not None
    lease.keep_renewed()
    try:
        await asyncio.sleep(1.0)  # past half the TTL from the take
        # the first hung and a later one was sent; held means one landed
        assert lease.held and ch.renewals >= 2
    finally:
        await lease.release()


async def test_renewals_that_all_hang_still_lose_the_lease_at_half_its_ttl() -> None:
    """Cutting each attempt short never stretches the window: a lease whose every
    renewal hangs is lost half the TTL after its last landed renewal."""

    class AlwaysHangs(FakeLockTable):
        async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
            if sql == leases.LOCK_RENEW:
                await asyncio.sleep(10)
            await super().execute(sql, params, **kw)

    ch = AlwaysHangs()
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(leases.LeaseLost):
        async with leases.cluster_lease("job", ttl_s=0.6, ch=ch):
            await asyncio.sleep(2)
    assert 0.25 <= loop.time() - started < 1.0  # at half the TTL, long before the 2 s block
