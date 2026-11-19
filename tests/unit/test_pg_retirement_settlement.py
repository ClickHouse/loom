"""A native certificate includes positive settlement of authority Postgres work."""

from __future__ import annotations

import asyncio
import sys
import time
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.cli import cell_control
from server.core import pg, provider_process, retirement
from server.insights.app import provider_barrier as barrier
from server.insights.app import provider_command as command
from server.insights.store import provider_participants as store
from server.settings import settings

pytestmark = pytest.mark.asyncio
DEPLOYMENT = "00000000-0000-0000-0000-000000000019"


class Session:
    def __init__(self, world, pid, startup):
        self.world, self.pid, self.startup = world, pid, startup
        self.application = startup["application_name"]
        self.metadata = False
        self.closed = False
        self.aborted = False

    async def set_type_codec(self, *args, **kwargs):
        pass

    async def execute(self, sql, *args, **kwargs):
        assert sql == "COMMIT"
        self.world.commits.append(self.pid)
        if self.world.commit_entered is not None:
            self.world.commit_entered.set()
            await self.world.commit_finish.wait()
        if self.world.operation_error is not None:
            raise self.world.operation_error
        return "COMMIT"

    async def fetchrow(self, sql, *args, **kwargs):
        assert "FROM loom.authority" in sql
        self.metadata = True
        return self.world.pg_record

    async def fetchval(self, sql, tag, legacy, **kwargs):
        assert "FROM pg_stat_activity" in sql
        assert "pid <> pg_backend_pid()" in sql
        assert "state" not in sql and "query_start" not in sql and "xact_start" not in sql
        self.metadata = True
        self.world.censuses.append((tag, legacy, self.pid))
        if self.world.count_override is not ...:
            return self.world.count_override
        return sum(
            peer.pid != self.pid
            and (peer.application == tag or (legacy and peer.application == "loom"))
            for peer in self.world.sessions.values()
        )

    async def close(self, **kwargs):
        self.world.closes.append((self.pid, kwargs.get("timeout")))
        if not self.metadata and not self.world.application_close_ack:
            raise self.world.close_error
        self.closed = True
        self.world.sessions.pop(self.pid, None)

    def terminate(self):
        # Losing the local transport does not acknowledge remote COMMIT/session exit.
        self.aborted = True

    def is_closed(self):
        return self.closed or self.aborted


class World:
    def __init__(self):
        self.ch_record = {"authority": "postgres", "deployment_id": DEPLOYMENT, "epoch": 1}
        self.pg_record = {**self.ch_record, "in_recovery": False}
        self.sessions = {}
        self.commits, self.closes, self.censuses, self.rows = [], [], [], []
        self.application_close_ack = True
        self.operation_error = None
        self.close_error = OSError("public close acknowledgement lost")
        self.count_override = ...
        self.next_pid = 100
        self.commit_entered = self.commit_finish = None

    async def connect(self, *args, **kwargs):
        self.next_pid += 1
        conn = Session(self, self.next_pid, kwargs["server_settings"])
        self.sessions[conn.pid] = conn
        return conn

    async def query_one(self, sql, *args, **kwargs):
        if "FROM loom.storage_authority" in sql:
            return self.ch_record
        assert "system.processes" in sql
        return {"running": 0}

    async def query_all(self, *args, **kwargs):
        return []

    async def insert(self, table, rows, **kwargs):
        assert table == store.TABLE
        self.rows.extend(dict(row) for row in rows)


@pytest.fixture
def world(monkeypatch):
    import asyncpg

    from server.core import ch, rmt_carry
    from server.core.model_work_gate import ModelWorkGate
    from server.memory.app import embedder_calibration

    world = World()
    gate = retirement.get_gate()

    async def enroll():
        gate.enrolled = True

    monkeypatch.setattr(asyncpg, "connect", world.connect)
    monkeypatch.setattr(pg, "_pools", weakref.WeakKeyDictionary())
    monkeypatch.setattr(pg, "_opening", weakref.WeakKeyDictionary())
    monkeypatch.setattr(pg, "_expected", None)
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgres://test@invalid/authority")
    monkeypatch.setattr(settings, "native_command_drain_timeout_s", 0.04)
    monkeypatch.setattr(ch, "get_client", lambda: world)
    monkeypatch.setattr(command, "get_client", lambda: world)
    monkeypatch.setattr(store, "_default_client", lambda: world)
    monkeypatch.setattr(command, "register_process", enroll)
    monkeypatch.setattr(barrier, "_OWN", barrier._Own())
    monkeypatch.setattr(
        barrier, "_HEARTBEAT", SimpleNamespace(signal_stop=lambda: None, running=False)
    )
    monkeypatch.setattr(barrier, "get_model_work_gate", lambda: ModelWorkGate())
    monkeypatch.setattr(rmt_carry, "unsettled", lambda: 0)
    monkeypatch.setattr(embedder_calibration, "stop_refresh_loop", lambda: None)
    for module in (barrier, command):
        monkeypatch.setattr(module, "drain_task_sets", AsyncMock(return_value=True))
        monkeypatch.setattr(module, "flush_registered_buffers", AsyncMock(return_value=True))
    return world


async def owned_command(world):
    owner = cell_control.Owner()

    async def opening():
        await owner.connect(statement_timeout_ms=60_000)

    async def work():
        await owner.conn.execute("COMMIT")
        return 7

    try:
        return await owner.run(opening, work, opening_s=0.2)
    finally:
        assert not owner._tasks or all(task.done() for task in owner._tasks)
        assert owner._close_task is not None and owner._close_task.done()


def assert_certificate(world):
    assert len(world.rows) == 1
    row = world.rows[0]
    assert row["participant_id"] == provider_process.process_identity()
    assert row["transition_id"] == UUID(int=0)
    assert row["state"] == "retired" and row["stale_after_s"] == 0
    assert type(row["version"]) is int and row["version"] > 0
    assert provider_process.has_certificate(row["host_proof"])


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_real_owner_and_native_completion_preserve_first_error_and_settlement(
    world, acknowledged
):
    world.application_close_ack = acknowledged
    first = world.operation_error = ValueError("original COMMIT outcome unknown")
    with pytest.raises(ValueError) as found:
        async with command.participant_command():
            await owned_command(world)
    assert found.value is first
    assert len(world.commits) == 1 and world.closes
    if acknowledged:
        assert_certificate(world)
    else:
        assert not world.rows, "an aborted local session cannot certify remote settlement"


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_actual_owner_cancellation_keeps_first_identity_through_native_completion(
    world, monkeypatch, acknowledged
):
    world.application_close_ack = acknowledged
    world.commit_entered, world.commit_finish = asyncio.Event(), asyncio.Event()
    first = []
    close = cell_control.Owner.close

    async def observe_close(owner):
        current = sys.exception()
        if isinstance(current, asyncio.CancelledError) and not first:
            first.append(current)
        await close(owner)

    async def composed():
        try:
            async with command.participant_command():
                await owned_command(world)
        except asyncio.CancelledError as error:
            return error  # Keep Task's final cancellation conversion outside this observation.
        pytest.fail("the caller's cancellation must leave the command")

    monkeypatch.setattr(cell_control.Owner, "close", observe_close)
    task = asyncio.create_task(composed())
    try:
        await world.commit_entered.wait()
        task.cancel("original owner cancellation")
        result = await task
        assert first and result is first[0]
        assert str(result) == "original owner cancellation"
        if acknowledged:
            assert_certificate(world)
        else:
            assert not world.rows, "caller cancellation cannot certify unknown backend exit"
    finally:
        world.commit_finish.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_handled_control_error_cannot_turn_unknown_close_into_a_certificate(
    world, acknowledged
):
    world.application_close_ack = acknowledged
    first = world.operation_error = ValueError("handled command failure")

    async def attach_style_command():
        async with command.participant_command():
            try:
                await owned_command(world)
            except ValueError as found:
                assert found is first
                result = 1
        return result

    if acknowledged:
        assert await attach_style_command() == 1
        assert_certificate(world)
    else:
        with pytest.raises(TimeoutError):
            await attach_style_command()
        assert not world.rows


@pytest.mark.parametrize("acknowledged", [False, True])
async def test_successful_operation_still_requires_known_owned_close(world, acknowledged):
    world.application_close_ack = acknowledged
    if acknowledged:
        async with command.participant_command():
            assert await owned_command(world) == 7
        assert_certificate(world)
    else:
        with pytest.raises(OSError) as found:
            async with command.participant_command():
                await owned_command(world)
        assert found.value is world.close_error
        assert not world.rows, "required close failure leaves remote work unproved"


async def test_later_positive_backend_absence_can_resolve_an_unknown_close(world):
    world.application_close_ack = False
    first = world.operation_error = ValueError("unknown operation")
    with pytest.raises(ValueError) as found:
        async with command.participant_command():
            await owned_command(world)
    assert found.value is first
    assert not world.rows, "the first unknown attempt must remain uncertified"
    world.sessions.clear()  # The authority now positively reports backend absence.
    assert await barrier._retire_until(world, time.monotonic() + 0.2)
    assert_certificate(world)


async def test_owner_application_child_stays_owned_after_bounded_cancellation(world):
    gate = retirement.get_gate()
    gate.install("command")
    gate.enrolled = True
    entered, finish = asyncio.Event(), asyncio.Event()
    owner = cell_control.Owner()

    async def work():
        entered.set()
        while not finish.is_set():
            try:
                await finish.wait()
            except asyncio.CancelledError:
                pass

    task = owner._start(work)
    try:
        await entered.wait()
        owner.close_end = time.monotonic()
        await owner._stop({task})
        assert not task.done()
        gate.stopped()
        with pytest.raises(TimeoutError):
            await barrier._retire_until(world, time.monotonic() + 0.02)
        assert not world.rows, "a pending application child can still issue work"
    finally:
        finish.set()
        await task


@pytest.mark.parametrize(
    "invalid", ["missing-record", "wrong-mode", "bad-epoch", "wrong-pair", "standby", "bad-count"]
)
async def test_retirement_requires_fresh_valid_authority_and_backend_evidence(world, invalid):
    if invalid == "missing-record":
        world.ch_record = None
    elif invalid == "wrong-mode":
        world.ch_record = {**world.ch_record, "authority": "clickhouse"}
    elif invalid == "bad-epoch":
        world.ch_record = {**world.ch_record, "epoch": True}
    elif invalid == "wrong-pair":
        world.pg_record = {**world.pg_record, "deployment_id": str(UUID(int=20))}
    elif invalid == "standby":
        world.pg_record = {**world.pg_record, "in_recovery": True}
    else:
        world.count_override = False
    gate = retirement.get_gate()
    gate.install("command")
    gate.enrolled = True
    gate.stopped()
    result = False
    try:
        result = await barrier._retire_until(world, time.monotonic() + 0.02)
    except (TimeoutError, pg.AuthorityMismatch, OSError, pg.PostgresUnavailableError):
        pass
    assert not world.rows, "unknown or contradictory authority evidence cannot certify"
    assert result is not True


async def test_valid_recorded_clickhouse_completion_needs_no_postgres(world, monkeypatch):
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    monkeypatch.setattr(settings, "pg_dsn", "")
    world.ch_record = {**world.ch_record, "authority": "clickhouse"}
    async with command.participant_command():
        pass
    assert not world.sessions and not world.censuses
    assert_certificate(world)
