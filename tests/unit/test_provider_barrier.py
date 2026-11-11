"""A drain acknowledgement, a retirement or an expired heartbeat with no query
still running permits rebuilding shared vectors; nothing else does."""

import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.core.ch import current_query_tag
from server.core.model_work_gate import ModelWorkGate
from server.insights.app import provider_barrier as barrier
from server.insights.store import provider_participants as pp
from server.settings import settings


def _row(pid, transition=None, state="running", *, version=1, expired=False):
    return {
        "participant_id": pid,
        "transition_id": transition or UUID(int=0),
        "state": state,
        "version": version,
        "heartbeat_at": "2026-01-01 00:00:00",
        "expired": expired,
    }


@pytest.fixture
def setup(monkeypatch):
    identity, transition = uuid4(), uuid4()
    gate = ModelWorkGate()
    monkeypatch.setattr(barrier, "get_model_work_gate", lambda: gate)
    monkeypatch.setattr(barrier, "process_identity", lambda: identity)
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(return_value=True))
    monkeypatch.setattr(barrier, "flush_registered_buffers", AsyncMock(return_value=True))
    record = AsyncMock()
    monkeypatch.setattr(barrier, "record_participant", record)
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    rows = [_row(identity, transition, "drained")]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    monkeypatch.setattr(barrier, "_OWN", barrier._Own())
    monkeypatch.setattr(barrier, "_STALE", None)
    monkeypatch.setattr(barrier, "_last_prune", None)
    monkeypatch.setattr(barrier, "prune_retired", AsyncMock(return_value=0))
    return identity, transition, gate, record


@pytest.mark.asyncio
async def test_active_request_prevents_ack(setup):
    _, transition, gate, record = setup
    async with gate.admit():
        assert not await barrier.acknowledge_drain(transition)
    assert gate.closed
    record.assert_not_awaited()
    assert await barrier.acknowledge_drain(transition)
    assert record.call_args.args[1:] == (transition, "drained")


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", ["carried", "insert_running", "uncountable"])
async def test_a_write_that_may_still_land_holds_the_acknowledgement_and_the_retirement(
    setup, monkeypatch, pending
):
    """A cancelled request leaves the gate and its task, but its insert can still run
    (or, carried, still start) on the server: neither ack nor retire while it may."""
    from server.core import rmt_carry

    _, transition, gate, record = setup
    if pending == "carried":
        monkeypatch.setattr(rmt_carry, "unsettled", lambda: 1)
    else:
        running = AsyncMock(return_value=1 if pending == "insert_running" else None)
        monkeypatch.setattr(barrier, "running_queries", running)
    assert not await barrier.acknowledge_drain(transition)
    assert not await barrier.retire_process()
    record.assert_not_awaited()
    if pending != "carried":
        assert running.await_args.kwargs == {"inserts": True}


@pytest.mark.asyncio
async def test_buffer_flush_must_not_hide_a_new_deferred_writer(setup, monkeypatch):
    _, transition, gate, record = setup
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(side_effect=[True, False]))
    assert not await barrier.acknowledge_drain(transition)
    assert gate.closed
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_live_participant_must_ack_the_same_transition(setup, monkeypatch):
    identity, transition, _, record = setup
    rows = [_row(identity, transition, "drained"), _row(uuid4(), state="drained")]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    assert not await barrier.distributed_drain(transition)
    record.assert_not_awaited()
    rows[1]["transition_id"] = transition
    assert await barrier.distributed_drain(transition)


@pytest.mark.asyncio
async def test_an_expired_heartbeat_alone_never_lets_the_rebuild_through(setup, monkeypatch):
    """A participant that stopped beating may be paused with admitted work outside
    ClickHouse: the transition waits until it drains or an operator retires it."""
    identity, transition, _, record = setup
    paused = uuid4()
    rows = [_row(identity, transition, "drained"), _row(paused, expired=True, version=100)]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    assert not await barrier.distributed_drain(transition)
    record.assert_not_awaited()
    rows[1] = _row(paused, transition, "drained")  # it resumed and drained
    assert await barrier.distributed_drain(transition)


@pytest.mark.asyncio
async def test_an_unknown_query_census_never_counts_as_stopped(setup, monkeypatch):
    identity, _, _, record = setup
    quiet = uuid4()
    rows = [_row(identity), _row(quiet, expired=True)]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=None))
    assert await barrier.retire_participant(quiet) == "live"
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_paused_participant_that_beats_again_blocks_the_drain(setup, monkeypatch):
    identity, transition, _, _ = setup
    rows = [_row(identity, transition, "drained"), _row(uuid4(), expired=True)]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    assert not await barrier.distributed_drain(transition)


@pytest.mark.asyncio
async def test_this_process_never_retires_itself(setup, monkeypatch):
    identity, transition, _, record = setup
    monkeypatch.setattr(
        barrier, "participants", AsyncMock(return_value=[_row(identity, expired=True)])
    )
    assert not await barrier.distributed_drain(transition)
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_census_or_missing_coordinator_is_not_a_barrier(setup, monkeypatch):
    _, transition, _, _ = setup
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=[]))
    assert not await barrier.distributed_drain(transition)


@pytest.mark.asyncio
async def test_retirement_refuses_pending_writers(setup, monkeypatch):
    _, _, _, record = setup
    monkeypatch.setattr(barrier, "drain_task_sets", AsyncMock(return_value=False))
    assert not await barrier.retire_process()
    record.assert_not_awaited()


class _SyncCH:
    """The heartbeat's sync client."""

    def __init__(self, *, seen=0):
        self.seen = seen
        self.inserts: list[tuple[list, list]] = []

    def query(self, sql, parameters=None, settings=None):
        assert sql == pp.OWN_VERSION_SQL and settings["select_sequential_consistency"] == 1
        return SimpleNamespace(result_rows=[(self.seen,)])

    def insert(self, table, data, column_names, settings=None):
        self.inserts.append((column_names, data[0]))


def test_a_heartbeat_carries_the_latest_state_above_any_version_seen(setup):
    identity, transition, _, _ = setup
    ch = _SyncCH(seen=10**20)
    beat = barrier._Heartbeat(lambda: ch)
    beat.ch = ch
    barrier._OWN.stamp(transition, "drained")
    beat.beat()
    cols, row = ch.inserts[-1]
    assert dict(zip(cols, row, strict=True)) == {
        "participant_id": identity,
        "transition_id": transition,
        "state": "drained",
        "version": 10**20 + 1,
        "stale_after_s": barrier.STALE_AFTER_S,
        "host_proof": barrier.host_proof(),
    }
    # An acknowledgement stamped after the beat outranks it.
    assert barrier._OWN.stamp(transition, "drained")[2] > row[3]


@pytest.mark.asyncio
async def test_register_writes_the_first_row_and_retire_stops_the_beats(setup, monkeypatch):
    _, _, _, record = setup
    ch = _SyncCH()
    beat = barrier._Heartbeat(lambda: ch)
    monkeypatch.setattr(barrier, "_HEARTBEAT", beat)
    monkeypatch.setattr(barrier, "_registered", False)
    await barrier.register_process()
    await barrier.register_process()
    assert beat.running and len(ch.inserts) == 1
    tags: list[str] = []
    record.side_effect = lambda *_a, **_k: tags.append(current_query_tag())
    assert await barrier.retire_process()
    assert beat.stopping.is_set()  # signalled, not joined: the thread ends after its beat
    beat.thread.join(timeout=5)
    assert not beat.running
    assert record.call_args.args[2] == "retired"
    assert tags == ["provider.participant_heartbeat"] and current_query_tag() == ""
    beat.ch = ch
    beat.beat()
    assert len(ch.inserts) == 1  # a retired process never enrolls again by itself


@pytest.mark.asyncio
async def test_a_boot_that_fails_after_enrolling_retires_its_row(setup, monkeypatch):
    _, _, _, record = setup
    monkeypatch.setattr(barrier, "_HEARTBEAT", barrier._Heartbeat(lambda: _SyncCH()))
    monkeypatch.setattr(barrier, "_registered", False)

    @asynccontextmanager
    async def lifespan(_app):
        await barrier.register_process()
        raise RuntimeError("schema migrations failed on boot")
        yield

    with pytest.raises(RuntimeError, match="migrations failed"):
        async with barrier.participant_lifespan(lifespan)(None):
            pytest.fail("a failed boot never serves")
    assert record.call_args.args[2] == "retired"


@pytest.mark.asyncio
async def test_a_clean_shutdown_is_not_retired_twice(setup, monkeypatch):
    _, _, _, record = setup
    monkeypatch.setattr(barrier, "_registered", True)

    @asynccontextmanager
    async def lifespan(_app):
        yield

    async with barrier.participant_lifespan(lifespan)(None):
        pass
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_retire_participant_refuses_a_live_one(setup, monkeypatch):
    identity, _, _, record = setup
    other = uuid4()
    rows = [_row(identity), _row(other, version=5)]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    assert await barrier.retire_participant(uuid4()) == "absent"
    assert await barrier.retire_participant(identity) == "live"
    assert await barrier.retire_participant(other) == "live"
    rows[1]["expired"] = True
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=2))
    assert await barrier.retire_participant(other) == "live"
    record.assert_not_awaited()
    monkeypatch.setattr(barrier, "running_queries", AsyncMock(return_value=0))
    assert await barrier.retire_participant(other) == "retired"
    record.assert_awaited_once_with(other, UUID(int=0), "retired", after=5, ch=None)


def test_loom_participant_retire_refuses_a_live_one(setup, monkeypatch, capsys):
    import argparse
    import asyncio

    from server.cli import _ch, participant

    identity, _, _, record = setup
    other = uuid4()
    monkeypatch.setattr(
        barrier, "participants", AsyncMock(return_value=[_row(identity), _row(other)])
    )
    monkeypatch.setattr(_ch, "run_connected", lambda run, **_declared: asyncio.run(run(None)))
    args = argparse.Namespace(participant=str(other), yes=True)
    assert participant._cmd_retire(args) == 1
    assert "still running" in capsys.readouterr().err
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_retires_nobody_and_lists_what_blocks(setup, monkeypatch):
    identity, _, _, record = setup
    expired, busy, live = uuid4(), uuid4(), uuid4()
    rows = [
        _row(identity),
        _row(expired, expired=True, version=7),
        _row(busy, expired=True),
        _row(live),
    ]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        barrier, "running_queries", AsyncMock(side_effect=lambda p: int(str(p) == str(busy)))
    )
    assert barrier.stale_participants() is None
    await barrier.sweep_stopped(now=1000.0)
    record.assert_not_awaited()
    assert [(p["participant_id"], p["reason"]) for p in barrier.stale_participants()] == [
        (str(expired), "heartbeat_expired"),
        (str(busy), "queries_running"),
    ]
    barrier.running_queries.reset_mock()
    await barrier.sweep_stopped(now=1000.0 + barrier.SWEEP_S / 2)
    barrier.running_queries.assert_not_awaited()  # throttled


@pytest.mark.asyncio
async def test_the_sweep_deletes_long_retired_participants_hourly(setup):
    for t in (0.0, barrier.SWEEP_S, barrier.PRUNE_S):
        await barrier.sweep_stopped(now=1000.0 + t)
    assert barrier.prune_retired.await_count == 2


class _Running:
    """system.processes answers: `fail` is what the cluster read raises."""

    def __init__(self, fail: Exception | None) -> None:
        self.fail = fail
        self.sql: list[str] = []

    async def query_one(self, sql, params=None):
        self.sql.append(sql)
        if "clusterAllReplicas" in sql and self.fail is not None:
            raise self.fail
        return {"running": 3}


@pytest.mark.asyncio
async def test_only_a_missing_cluster_falls_back_to_the_local_server(monkeypatch):
    from server.insights.store import provider_participants as pp

    no_cluster = RuntimeError("Code: 701. DB::Exception: Requested cluster 'default' not found")
    ch = _Running(no_cluster)
    assert await pp.running_queries(uuid4(), ch=ch) == 3
    assert ch.sql[-1] == pp.RUNNING_LOCAL_SQL
    # A replica that does not answer may run the participant's queries: busy.
    ch = _Running(RuntimeError("Code: 279. ALL_CONNECTION_TRIES_FAILED"))
    assert await pp.running_queries(uuid4(), ch=ch) is None
    assert len(ch.sql) == 1
    monkeypatch.setattr(settings, "ch_cluster", "")
    ch = _Running(no_cluster)
    assert await pp.running_queries(uuid4(), ch=ch) == 3
    assert ch.sql == [pp.RUNNING_LOCAL_SQL]


def test_heartbeat_thread_keeps_beating_through_a_failed_beat(setup, monkeypatch):
    monkeypatch.setattr(barrier, "HEARTBEAT_S", 0.01)
    beats = threading.Semaphore(0)

    class Flaky(_SyncCH):
        calls = 0

        def insert(self, *a, **k):
            Flaky.calls += 1
            beats.release()
            if Flaky.calls == 2:
                raise RuntimeError("connection reset")
            super().insert(*a, **k)

    beat = barrier._Heartbeat(Flaky)
    beat.start()
    for _ in range(4):
        assert beats.acquire(timeout=5)
    beat.stop()
    assert not beat.running


@pytest.mark.asyncio
async def test_shutdown_retries_the_retirement_until_a_cut_off_write_settles(setup, monkeypatch):
    """A write cancelled mid-insert holds the retirement; shutdown settles and retries
    instead of exiting unretired, which would block every reconfiguration."""
    from server.core import rmt_carry

    settles: list[int] = []
    left = iter([1, 1, 0])
    monkeypatch.setattr(rmt_carry, "unsettled", lambda: next(left, 0))
    monkeypatch.setattr(rmt_carry, "settle", AsyncMock(side_effect=lambda ch: settles.append(1)))
    monkeypatch.setattr(barrier.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(barrier, "_HEARTBEAT", barrier._Heartbeat(lambda: None))
    assert await barrier.retire_at_shutdown(object(), budget_s=60)
    assert len(settles) == 2


@pytest.mark.asyncio
async def test_shutdown_gives_up_within_its_budget_and_says_how_to_retire(
    setup, monkeypatch, caplog
):
    from server.core import rmt_carry

    monkeypatch.setattr(rmt_carry, "unsettled", lambda: 1)
    monkeypatch.setattr(rmt_carry, "settle", AsyncMock())
    with caplog.at_level("WARNING"):
        assert not await barrier.retire_at_shutdown(object(), budget_s=0)
    assert "loom participant retire --participant" in caplog.text


def test_the_heartbeat_insert_never_holds_its_own_retirement() -> None:
    from server.insights.store import provider_participants as pp_store

    sql = pp_store.running_sql(inserts=True)
    assert "query_kind = 'Insert'" in sql
    # A prefix: the statement digest may be appended to the tag.
    assert "NOT startsWith(Settings['log_comment'], 'provider.participant_heartbeat')" in sql
    assert barrier._TAG["log_comment"] == "provider.participant_heartbeat"


@pytest.mark.asyncio
@pytest.mark.parametrize("slow", ["census", "settle"])
async def test_shutdown_retirement_never_outlives_its_budget(setup, monkeypatch, slow):
    """A census or a settle that hangs is cut off at the budget, so the stop grace
    still leaves room to withdraw and disconnect."""
    import asyncio
    import time

    from server.core import rmt_carry

    async def _hang(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(rmt_carry, "unsettled", lambda: 1 if slow == "settle" else 0)
    if slow == "census":
        monkeypatch.setattr(barrier, "running_queries", _hang)
    monkeypatch.setattr(rmt_carry, "settle", _hang if slow == "settle" else AsyncMock())
    started = time.monotonic()
    assert not await barrier.retire_at_shutdown(object(), budget_s=0.05)
    assert time.monotonic() - started < 0.5


@pytest.mark.asyncio
async def test_a_live_row_expires_after_the_heartbeat_window_and_a_retired_one_at_once():
    ch = SimpleNamespace(insert=AsyncMock())
    await pp.record_participant(uuid4(), UUID(int=0), "running", ch=ch)
    (row,) = ch.insert.call_args.args[1]
    assert row["stale_after_s"] == pp.STALE_AFTER_S > barrier.HEARTBEAT_S
    await pp.record_participant(uuid4(), UUID(int=0), "retired", ch=ch)
    (row,) = ch.insert.call_args.args[1]
    assert row["stale_after_s"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", [0, 1])
async def test_boot_enrolls_before_migrating_only_once_the_heartbeat_columns_exist(
    monkeypatch, columns
):
    """A database whose migrations stopped before the heartbeat columns would
    refuse the first heartbeat and fail the boot before it could migrate."""
    register = AsyncMock()
    monkeypatch.setattr(barrier, "register_process", register)
    ch = SimpleNamespace(query_one=AsyncMock(return_value={"n": columns}))
    await barrier.enroll_before_migrations(ch)
    assert "host_proof" in ch.query_one.call_args.args[0]
    assert register.await_count == columns
