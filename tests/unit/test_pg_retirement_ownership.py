"""Retirement owns local Postgres work and proves remote backend absence."""

from __future__ import annotations

import asyncio
import os
import time
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.core import pg, provider_process, retirement
from server.insights.app import provider_barrier as barrier
from tests.unit import test_pg_retirement_settlement as settlement

pytestmark = pytest.mark.asyncio
settlement_world = settlement.world
TARGET = UUID(int=711)
RAW_PROOF = "test-host|test-boot||test-pidns|711|10||"


class Backend:
    """The server retains a session until its public close acknowledges exit."""

    def __init__(self, world, pid, startup, *, verifier=False):
        self.world, self.pid = world, pid
        self.startup = dict(startup)
        self.application = startup["application_name"]
        self.state = "idle"
        self.verifier = verifier
        self.closed = self.aborted = False

    async def set_type_codec(self, *args, **kwargs):
        pass

    async def fetchrow(self, sql, *args, **kwargs):
        assert self.verifier and "FROM loom.authority" in sql
        return self.world.pg_record

    async def fetchval(self, sql, tag, legacy, **kwargs):
        assert self.verifier and "FROM pg_stat_activity" in sql
        assert "pid <> pg_backend_pid()" in sql
        assert "state" not in sql and "datname" not in sql
        self.world.censuses.append((tag, legacy, self.pid))
        return sum(
            peer.pid != self.pid
            and (peer.application == tag or (legacy and peer.application == "loom"))
            for peer in self.world.sessions.values()
        )

    async def close(self, **kwargs):
        self.world.closes.append((self.pid, kwargs.get("timeout")))
        if self.verifier and not self.world.verifier_close_ack:
            raise OSError("verifier close acknowledgement lost")
        self.closed = True
        self.world.sessions.pop(self.pid, None)

    def terminate(self):
        self.aborted = True

    def is_closed(self):
        return self.closed or self.aborted


@pytest.fixture
def ownership(settlement_world, monkeypatch):
    import asyncpg

    world = settlement_world
    world.participant_rows = []
    world.verifier_close_ack = True

    async def connect(*args, **kwargs):
        world.next_pid += 1
        conn = Backend(
            world,
            world.next_pid,
            kwargs["server_settings"],
            verifier="timeout" in kwargs,
        )
        world.sessions[conn.pid] = conn
        return conn

    async def query_all(sql, params=None, **kwargs):
        assert "FROM loom._provider_participants" in sql
        rows = world.participant_rows
        return [r for r in rows if r["host_proof"] == params["proof"]] if params else rows

    def missing_process(pid, signal):
        assert pid == 711 and signal == 0
        raise ProcessLookupError

    pid = os.getpid()
    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(world, "query_all", query_all)
    monkeypatch.setattr(pg, "_closing", weakref.WeakKeyDictionary())
    monkeypatch.setattr(pg, "_state_pid", pid)
    monkeypatch.setattr(barrier, "_default_client", lambda: world)
    monkeypatch.setattr(barrier, "_last_here", -float("inf"))
    monkeypatch.setattr(barrier, "_last_sweep", 0.0)
    monkeypatch.setattr(barrier, "_last_prune", 1000.0)
    monkeypatch.setattr(barrier, "_STALE", None)
    monkeypatch.setattr(provider_process, "_boot_and_pidns", lambda: ("test-boot", "test-pidns"))
    monkeypatch.setattr(
        provider_process, "socket", SimpleNamespace(gethostname=lambda: "test-host")
    )
    monkeypatch.setattr(
        provider_process, "os", SimpleNamespace(getpid=lambda: pid, kill=missing_process)
    )
    monkeypatch.setattr(
        provider_process, "_proof", (pid, f"test-host|test-boot||test-pidns|{pid}|1||")
    )
    return world


def target_row(world, *, legacy=False):
    proof = RAW_PROOF if legacy else provider_process.certified(RAW_PROOF)
    row = {
        "participant_id": TARGET,
        "transition_id": UUID(int=0),
        "state": "retired" if legacy else "running",
        "version": 17,
        "host_proof": proof,
        "expired": True,
        "heartbeat_at": "fixture heartbeat",
    }
    world.participant_rows[:] = [row]
    return row


def backend(world, application):
    world.next_pid += 1
    conn = Backend(world, world.next_pid, {"application_name": application})
    world.sessions[conn.pid] = conn
    return conn


def stopped_gate():
    gate = retirement.get_gate()
    gate.install("command")
    gate.enrolled = True
    gate.stopped()
    return gate


async def recover(path, world, *, end):
    if path == "manual":
        return await barrier.retire_participant(TARGET, ch=world, end=end)
    if path == "reaped":
        return await barrier.retire_reaped_process(world.participant_rows[0]["host_proof"], end)
    if path == "dead-here":
        return await barrier.retire_dead_here(end=end, now=1000.0)
    assert path == "sweep"
    return await barrier.sweep_stopped(end=end, now=1000.0)


@pytest.mark.parametrize("path", ["manual", "reaped", "dead-here", "sweep"])
@pytest.mark.parametrize("backend_live", [False, True])
async def test_every_death_recovery_path_requires_positive_backend_absence(
    ownership, path, backend_live
):
    world = ownership
    target_row(world)
    backend(world, provider_process.participant_tag(UUID(int=712)))
    if backend_live:
        backend(world, provider_process.participant_tag(TARGET))
    await recover(path, world, end=time.monotonic() + 0.2)
    assert world.censuses and all(not legacy for _, legacy, _ in world.censuses)
    assert len(world.rows) == (0 if backend_live else 1)
    if world.rows:
        row = world.rows[0]
        assert row["participant_id"] == TARGET and row["state"] == "retired"
        assert row["version"] > 17 and row["stale_after_s"] == 0
        assert provider_process.has_certificate(row["host_proof"])
    if path == "sweep":
        assert bool(barrier.stale_participants()) is backend_live


@pytest.mark.parametrize("generic_idle", [False, True])
async def test_raw_legacy_retired_row_waits_for_all_generic_sessions(ownership, generic_idle):
    world = ownership
    target_row(world, legacy=True)
    backend(world, provider_process.participant_tag(UUID(int=712)))
    if generic_idle:
        backend(world, "loom")
    result = await barrier.retire_participant(TARGET, ch=world, end=time.monotonic() + 0.2)
    assert result == ("live" if generic_idle else "retired")
    assert len(world.rows) == (0 if generic_idle else 1)
    assert world.censuses and all(legacy for _, legacy, _ in world.censuses)


async def test_raw_clickhouse_recovery_requires_no_postgres_io(ownership, monkeypatch):
    import asyncpg

    from server.settings import settings

    world = ownership
    target_row(world, legacy=True)
    world.ch_record = {**world.ch_record, "authority": "clickhouse"}
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(settings, "pg_dsn", "")
    connect = AsyncMock(side_effect=AssertionError("CH recovery cannot contact PG"))
    monkeypatch.setattr(asyncpg, "connect", connect)
    assert (
        await barrier.retire_participant(TARGET, ch=world, end=time.monotonic() + 0.2) == "retired"
    )
    assert len(world.rows) == 1 and not world.censuses
    connect.assert_not_awaited()


class Pool:
    def __init__(self, conn=None, *, fail=False, finish=None):
        self.conn, self.fail, self.finish = conn, fail, finish
        self.close_calls = self.terminations = 0
        self.entered = asyncio.Event()
        self.releases = []

    async def close(self):
        self.close_calls += 1
        self.entered.set()
        if self.fail:
            raise OSError("pool public close failed")
        if self.finish is not None:
            while not self.finish.is_set():
                try:
                    await self.finish.wait()
                except asyncio.CancelledError:
                    pass

    def terminate(self):
        self.terminations += 1

    async def release(self, conn, **kwargs):
        self.releases.append(conn)


async def test_failed_close_is_retained_until_later_positive_census(ownership):
    world = ownership
    gate = stopped_gate()
    gate.pg_sealed = True
    loop = asyncio.get_running_loop()
    pool = Pool(fail=True)
    pg._pools[loop] = pool
    peer = backend(world, provider_process.participant_tag(provider_process.process_identity()))
    assert not await barrier.retire_process(world, end=time.monotonic() + 0.2)
    child = pg._closing[loop]
    assert child.done() and pg._pools[loop] is pool
    assert pool.close_calls == 1 and not world.rows
    world.sessions.pop(peer.pid)
    assert await barrier.retire_process(world, end=time.monotonic() + 0.2)
    assert len(world.rows) == 1 and pool.close_calls == 1
    assert loop not in pg._pools and loop not in pg._closing


async def test_pending_resistant_close_keeps_one_child_and_ownership(ownership):
    world = ownership
    gate = stopped_gate()
    gate.pg_sealed = True
    loop = asyncio.get_running_loop()
    finish = asyncio.Event()
    pool = Pool(finish=finish)
    pg._pools[loop] = pool
    try:
        assert not await pg.close_pool_until(end=time.monotonic() + 0.01)
        child = pg._closing[loop]
        assert not child.done() and gate.active == 1
        assert not await pg.close_pool_until(end=time.monotonic())
        assert pg._closing[loop] is child and pool.close_calls == 1
        assert not gate.quiescent and not world.rows
    finally:
        finish.set()
        if loop in pg._closing:
            await asyncio.gather(pg._closing[loop], return_exceptions=True)
        await asyncio.sleep(0)
    assert gate.quiescent and pg._pools[loop] is pool
    assert await barrier.retire_process(world, end=time.monotonic() + 0.2)
    assert len(world.rows) == 1 and pool.close_calls == 1


async def test_repeated_cancellation_preserves_first_error_and_original_close_end(ownership):
    entered, finish = asyncio.Event(), asyncio.Event()
    children, timeouts, aborted = [], [], []

    async def close(**kwargs):
        children.append(asyncio.current_task())
        timeouts.append(kwargs["timeout"])
        entered.set()
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                pass

    conn = SimpleNamespace(close=close, terminate=lambda: aborted.append(True))
    start = time.monotonic()
    end = start + 0.03
    task = asyncio.create_task(pg.close_owned(conn, end=end))
    try:
        await entered.wait()
        task.cancel("first close cancellation")
        await asyncio.sleep(0)
        task.cancel("second close cancellation")
        with pytest.raises(asyncio.CancelledError) as found:
            await task
        assert str(found.value) == "first close cancellation"
        assert len(timeouts) == 1 and 0 < timeouts[0] <= end - start
        assert aborted and len(children) == 1
    finally:
        finish.set()
        await asyncio.gather(task, *children, return_exceptions=True)


async def test_unpublished_pool_cannot_cross_terminal_seal(ownership, monkeypatch):
    import asyncpg

    gate = retirement.get_gate()
    entered, finish = asyncio.Event(), asyncio.Event()
    pool = Pool()

    async def create_pool(*args, **kwargs):
        entered.set()
        await finish.wait()
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    task = asyncio.create_task(pg._pool_for_loop())
    try:
        await entered.wait()
        assert gate.active == 1
        gate.pg_sealed = True
        finish.set()
        with pytest.raises(retirement.ProcessStopping):
            await task
        assert not pg._pools and pool.terminations == 1 and gate.active == 0
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("seal_before", [False, True])
async def test_pool_maintenance_callback_owns_initialization_and_refuses_seal(
    ownership, monkeypatch, seal_before
):
    import asyncpg

    gate = retirement.get_gate()
    entered, finish = asyncio.Event(), asyncio.Event()
    conn = backend(ownership, provider_process.participant_tag(provider_process.process_identity()))
    connects = []

    async def codec(*args, **kwargs):
        entered.set()
        await finish.wait()

    async def connect(*args, **kwargs):
        connects.append(True)
        return conn

    conn.set_type_codec = codec
    monkeypatch.setattr(asyncpg, "connect", connect)
    gate.pg_sealed = seal_before
    task = asyncio.create_task(pg._pool_connect())
    try:
        if not seal_before:
            await entered.wait()
            assert gate.active == 1
            gate.pg_sealed = True
        finish.set()
        with pytest.raises(retirement.ProcessStopping):
            await task
        assert gate.active == 0
        assert bool(connects) is (not seal_before)
        assert conn.aborted is (not seal_before)
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_acquire_waiter_crossing_seal_never_yields_application_connection(ownership):
    gate = retirement.get_gate()
    entered, finish = asyncio.Event(), asyncio.Event()
    conn = backend(ownership, provider_process.participant_tag(provider_process.process_identity()))
    pool = Pool(conn)
    yielded = []

    async def acquire(**kwargs):
        entered.set()
        await finish.wait()
        return conn

    async def use():
        async with pg._acquire(pool) as held:
            yielded.append(held)

    pool.acquire = acquire
    task = asyncio.create_task(use())
    try:
        await entered.wait()
        assert gate.active == 1
        gate.pg_sealed = True
        finish.set()
        with pytest.raises(retirement.ProcessStopping):
            await task
        assert not yielded and conn.aborted and pool.releases == [conn]
        assert gate.active == 0
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("waiting", ["connect", "codecs"])
async def test_dedicated_late_handoff_remains_owned_and_cannot_escape_seal(
    ownership, monkeypatch, waiting
):
    import asyncpg

    gate = retirement.get_gate()
    entered, finish = asyncio.Event(), asyncio.Event()
    conn = backend(ownership, provider_process.participant_tag(provider_process.process_identity()))

    async def codec(*args, **kwargs):
        if waiting == "codecs":
            entered.set()
            await finish.wait()

    async def connect(*args, **kwargs):
        if waiting == "connect":
            entered.set()
            await finish.wait()
        return conn

    conn.set_type_codec = codec
    monkeypatch.setattr(asyncpg, "connect", connect)
    task = asyncio.create_task(pg._connect_dedicated())
    try:
        await entered.wait()
        assert gate.active == 1
        gate.pg_sealed = True
        finish.set()
        with pytest.raises(retirement.ProcessStopping):
            await task
        assert conn.aborted and gate.active == 0
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("state", ["pool", "opening", "closing"])
async def test_child_pid_refuses_inherited_live_authority_ownership(ownership, monkeypatch, state):
    loop = asyncio.get_running_loop()
    lock, task = asyncio.Lock(), None
    if state == "pool":
        pg._pools[loop] = Pool()
    elif state == "opening":
        await lock.acquire()
        pg._opening[loop] = lock
    else:
        task = asyncio.create_task(asyncio.Event().wait())
        pg._closing[loop] = task
    monkeypatch.setattr(pg, "_state_pid", -1)
    try:
        with pytest.raises(retirement.ProcessStopping):
            await pg._pool_for_loop()
        assert not ownership.sessions and retirement.get_gate().active == 0
    finally:
        if lock.locked():
            lock.release()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_child_without_cached_pg_state_opens_under_fresh_identity(ownership, monkeypatch):
    parent_identity = UUID(int=712)
    old_gate = retirement.get_gate()
    old_gate.install("command")
    old_gate.stopped()
    old_gate.pg_sealed = True
    monkeypatch.setattr(retirement, "_pid", -1)
    monkeypatch.setattr(pg, "_state_pid", -1)
    monkeypatch.setattr(provider_process, "_identity", (-1, parent_identity))
    conn = await pg._connect_dedicated()
    gate = retirement.get_gate()
    assert gate is not old_gate and not gate.closed and not gate.pg_sealed
    assert gate.active == 0 and provider_process.process_identity() != parent_identity
    assert conn.application == provider_process.participant_tag(provider_process.process_identity())
    await conn.close()
    assert not ownership.sessions


@pytest.mark.parametrize("kind", ["pool", "unmarked-dedicated"])
async def test_pool_and_unmarked_startup_preserve_participant_attribution(
    ownership, monkeypatch, kind
):
    import asyncpg

    expected = provider_process.participant_tag(provider_process.process_identity())
    if kind == "pool":
        captured = []
        pool = Pool()

        async def create_pool(*args, **kwargs):
            captured.append(kwargs)
            return pool

        monkeypatch.setattr(asyncpg, "create_pool", create_pool)
        assert await pg._pool_for_loop() is pool
        assert captured[0]["server_settings"]["application_name"] == expected
        assert captured[0]["connect"] is pg._pool_connect and "init" not in captured[0]
    else:
        conn = await pg._connect_dedicated(marked=False)
        assert conn.application == expected
        assert pg.TENANCY_MARKER not in conn.startup
        assert conn.pid in ownership.sessions
        await conn.close()


async def test_closed_application_admission_still_allows_final_pg_drain_before_seal(ownership):
    gate = stopped_gate()
    conn = await pg._connect_dedicated()
    assert gate.closed and gate.active == 0 and not gate.pg_sealed
    await conn.close()
    gate.pg_sealed = True
    with pytest.raises(retirement.ProcessStopping):
        await pg._connect_dedicated()
    assert not ownership.sessions


async def test_other_live_event_loop_pool_is_not_silently_ignored_at_retirement(ownership):
    other = asyncio.new_event_loop()
    pool = Pool()
    pg._pools[other] = pool
    try:
        with pytest.raises(retirement.ProcessStopping):
            await pg.close_pool_until(end=time.monotonic() + 0.2)
        assert pg._pools[other] is pool and pool.close_calls == 0
    finally:
        pg._pools.pop(other)
        other.close()


@pytest.mark.parametrize(
    ("role", "closed"),
    [("command", True), ("command", False), ("server", True), ("worker", True), ("", False)],
)
async def test_outer_disconnect_never_repeats_terminal_pg_cleanup(
    ownership, monkeypatch, role, closed
):
    from server import boot
    from server.cli import _ch

    gate = retirement.get_gate()
    if role:
        gate.install(role)
    if closed:
        gate.close()
    close = AsyncMock()
    monkeypatch.setattr(pg, "close_pool", close)
    if role in ("server", "worker"):
        await boot.close_storage_authority()
    else:
        ch = SimpleNamespace(disconnect=AsyncMock())
        await _ch.disconnect(ch)
        ch.disconnect.assert_awaited_once_with()
    assert close.await_count == (0 if role and closed else 1)


async def test_manual_recovery_uses_its_explicit_ch_for_both_authorities(ownership, monkeypatch):
    target_row(ownership)

    def unrelated_default():
        raise AssertionError("explicit target cannot fall back to the process default")

    monkeypatch.setattr(barrier, "_default_client", unrelated_default)
    assert (
        await barrier.retire_participant(TARGET, ch=ownership, end=time.monotonic() + 0.2)
        == "retired"
    )
    assert len(ownership.rows) == 1 and ownership.censuses


async def test_verifier_close_failure_prevents_certificate_then_positive_close_recovers(ownership):
    target_row(ownership)
    ownership.verifier_close_ack = False
    assert (
        await barrier.retire_participant(TARGET, ch=ownership, end=time.monotonic() + 0.2) == "live"
    )
    assert not ownership.rows and ownership.sessions
    ownership.sessions.clear()
    ownership.verifier_close_ack = True
    assert (
        await barrier.retire_participant(TARGET, ch=ownership, end=time.monotonic() + 0.2)
        == "retired"
    )
    assert len(ownership.rows) == 1 and not ownership.sessions
