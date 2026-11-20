"""Native callers retain their first outcome while deleting their probe nonces."""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from server.cli import cell_attach, cell_control, cell_prepare
from server.core import leases, object_store, pg
from server.settings import settings
from tests.unit import test_cell_native_http_first_error as native_http
from tests.unit.clock_fake import ManualClock
from tests.unit.test_cell_attach import FakeConn
from tests.unit.test_cell_control import snapshot

exchange = native_http.exchange
observed = native_http.observed
FIRST_ERRORS = native_http.FIRST_ERRORS
CALLERS = ("prepare", "begin", "serving")
FATAL_ERRORS = (asyncio.CancelledError, SystemExit, KeyboardInterrupt)
CLEANUP_PHASES = ("postgres", "clickhouse", "bucket")
URL = httpx.URL("http://router.test")


@pytest.fixture
def nonce_edges(monkeypatch, exchange):
    state = SimpleNamespace(
        secondary=None,
        phase="postgres",
        write_first=False,
        events=[],
        normalized=[],
        cleanup_entered=asyncio.Event(),
        cleanup_block=False,
    )
    nonces = {"postgres": "pg", "clickhouse": "ch", "bucket": "bucket"}

    async def deletion(phase):
        state.events.append(phase)
        if phase == state.phase:
            state.cleanup_entered.set()
            if state.cleanup_block:
                await asyncio.Future()
            if state.secondary is not None:
                raise state.secondary

    async def postgres(*args):
        await deletion("postgres")

    async def clickhouse(*args):
        assert args[0] == leases.LOCK_RELEASE
        await deletion("clickhouse")

    async def read_object(location):
        return b"bucket"

    async def bucket(*args):
        await deletion("bucket")

    async def write(*args):
        if state.write_first:
            raise exchange.first

    monkeypatch.setattr(pg, "execute", postgres)
    monkeypatch.setattr(object_store, "root_prefix", lambda: "")
    monkeypatch.setattr(object_store, "read", read_object)
    monkeypatch.setattr(object_store, "delete", bucket)
    monkeypatch.setattr(cell_prepare, "_new_nonces", lambda: nonces.copy())
    monkeypatch.setattr(cell_prepare, "_write_nonces", write)
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "document_s3_bucket", "bucket-test")
    state.ch = SimpleNamespace(execute=clickhouse)
    return state


@pytest.fixture
def native_call(monkeypatch, nonce_edges, exchange):
    # Scheduling cannot consume the fake session's close budget.
    phase_clock = ManualClock()
    monkeypatch.setattr(cell_control, "time", phase_clock)
    monkeypatch.setattr(pg, "time", phase_clock)
    state = nonce_edges
    row = {**snapshot(), "is_first": False, "state": "attaching", "mode": "enforced"}
    exchange.answer = row.copy()
    conn = FakeConn()
    rows = SimpleNamespace(deployment_id="dep", first=None, tenancy_rows=0)
    monkeypatch.setattr(cell_prepare, "_read_rows", AsyncMock(return_value=rows))
    monkeypatch.setattr(cell_prepare, "_read_beats", AsyncMock(return_value=[]))
    monkeypatch.setattr(cell_prepare, "_beat_fields", lambda *args: [{}])
    monkeypatch.setattr(cell_prepare, "_state_rows", lambda rows: 0)
    monkeypatch.setattr(cell_prepare, "_prefix_objects", AsyncMock(return_value=0))
    monkeypatch.setattr(cell_prepare, "_answer_problem", lambda *args: "")
    monkeypatch.setattr(cell_prepare, "_own_rows", lambda *args: ({}, None))
    state.write_rows = AsyncMock(return_value=[])
    monkeypatch.setattr(cell_prepare, "_write_rows", state.write_rows)
    monkeypatch.setattr(cell_control, "claim", AsyncMock(side_effect=lambda conn, call, row: row))
    args = argparse.Namespace(
        cell="first",
        wait=0,
        host="first.example.test",
        credential_sha256="hash",
        probe_url="",
        ch_allow_user=[],
        ch_allow_user_directory=[],
        ch_allow_object=[],
        ch_allow_replica=[],
        pg_allow_role=[],
    )

    async def mutate(path, body):
        return await cell_control.request(URL, "first", None, path, body)

    run = SimpleNamespace(
        ch=state.ch, args=args, row=row, own_fp=({}, None), keys_fp="key", mutate=mutate
    )

    async def invoke(caller, owned):
        owner = cell_control.Owner(exit_s=0.5)
        direct = SimpleNamespace(
            conn=conn, transaction=lambda work: work(), call=lambda work, **kw: work()
        )

        async def work():
            if caller == "prepare":
                return await cell_prepare._prepare_locked(
                    state.ch, args, URL, None, owner if owned else direct
                )
            return await getattr(cell_attach.Run, caller)(run)

        async def opening():
            owner.conn = conn

        return await owner.run(opening, work, opening_s=0.5) if owned else await work()

    state.conn = conn
    state.invoke = invoke
    return state


@pytest.fixture(autouse=True)
def selected_normalization(monkeypatch, nonce_edges):
    original_prepare = cell_prepare.Refused
    original_control = cell_control._refused

    class PrepareRefused(original_prepare):
        def __init__(self, message):
            super().__init__(message)
            nonce_edges.normalized.append(self)

    def control_refused(message):
        selected = original_control(message)
        nonce_edges.normalized.append(selected)
        return selected

    monkeypatch.setattr(cell_prepare, "Refused", PrepareRefused)
    monkeypatch.setattr(cell_control, "_refused", control_refused)


def assert_first(state, exchange, caller, first_kind, owned, outcome):
    kind, result = outcome
    assert kind == "error"
    normalized = not state.write_first and (
        (caller == "prepare" and issubclass(first_kind, httpx.HTTPError))
        or (caller != "prepare" and issubclass(first_kind, (httpx.HTTPError, TimeoutError)))
    )
    if normalized:
        assert state.normalized and result is state.normalized[0]
    elif owned and first_kind is asyncio.CancelledError:
        # asyncio reconstructs cancellation when Owner retrieves its child Task.
        assert isinstance(result, asyncio.CancelledError)
    else:
        assert result is exchange.first
    assert state.write_rows.await_count == 0
    assert state.conn.executed.count("CLOSE") == int(owned)


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("owned", (False, True))
@pytest.mark.parametrize("first_kind", FIRST_ERRORS)
@pytest.mark.parametrize("cleanup_kind", (*FATAL_ERRORS, None))
@pytest.mark.parametrize("phase", CLEANUP_PHASES)
@pytest.mark.parametrize("http_exit_failure", (False, True))
async def test_http_first_outcome_survives_native_nonce_cleanup(
    native_call, exchange, caller, owned, first_kind, cleanup_kind, phase, http_exit_failure
):
    state = native_call
    state.phase = phase
    state.secondary = cleanup_kind("nonce-second") if cleanup_kind is not None else None
    exchange.first = first_kind("request-first")
    exchange.second = KeyboardInterrupt("http-exit-second") if http_exit_failure else None
    assert_first(
        state,
        exchange,
        caller,
        first_kind,
        owned,
        await observed(lambda: state.invoke(caller, owned)),
    )
    assert phase in state.events and len(exchange.exit_args) == 3


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("owned", (False, True))
@pytest.mark.parametrize("cleanup_kind", (*FATAL_ERRORS, ValueError, None))
@pytest.mark.parametrize("phase", CLEANUP_PHASES)
async def test_standalone_nonce_cleanup_keeps_cancellation_and_success_neighbors(
    native_call, exchange, caller, owned, cleanup_kind, phase
):
    state = native_call
    state.phase = phase
    state.secondary = cleanup_kind("nonce-only") if cleanup_kind is not None else None
    kind, result = await observed(lambda: state.invoke(caller, owned))
    if cleanup_kind in FATAL_ERRORS:
        assert kind == "error"
        if owned and cleanup_kind is asyncio.CancelledError:
            assert isinstance(result, asyncio.CancelledError)
        else:
            assert result is state.secondary
        assert state.write_rows.await_count == 0
    else:
        assert kind == "value"
        assert result == (
            0 if caller == "prepare" else None if caller == "begin" else exchange.answer
        )
        assert state.events == list(CLEANUP_PHASES)
        assert state.write_rows.await_count == int(caller == "prepare")
    assert state.conn.executed.count("CLOSE") == int(owned)


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("owned", (False, True))
@pytest.mark.parametrize("cleanup_kind", FATAL_ERRORS)
async def test_nonce_write_failure_stays_first(native_call, exchange, caller, owned, cleanup_kind):
    state = native_call
    state.write_first = True
    state.secondary = cleanup_kind("nonce-second")
    exchange.first = ValueError("write-first")
    assert_first(
        state,
        exchange,
        caller,
        ValueError,
        owned,
        await observed(lambda: state.invoke(caller, owned)),
    )
    assert exchange.events == [] and state.events == ["postgres"]


@pytest.mark.parametrize("caller", CALLERS)
async def test_repeated_cancellation_keeps_first_stop_through_nonce_deletion(
    native_call, exchange, caller
):
    state = native_call
    state.cleanup_block = True
    exchange.block = True
    task = asyncio.create_task(observed(lambda: state.invoke(caller, False)))
    try:
        await asyncio.wait_for(exchange.entered.wait(), 1)
        task.cancel("first-stop")
        await asyncio.wait_for(state.cleanup_entered.wait(), 1)
        task.cancel("second-stop")
        kind, result = await task
        assert kind == "error" and result is exchange.first
        assert isinstance(result, asyncio.CancelledError) and result.args == ("first-stop",)
        assert state.write_rows.await_count == 0
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
