"""A control owner cannot survive an uncertain commit or a lost lock session."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from server.cli import cell_control
from tests.unit.test_cell_attach import FakeConn


@pytest.mark.asyncio
async def test_uncertain_commit_closes_before_cancelling_work(monkeypatch):
    class Stalled(FakeConn):
        async def execute(self, sql: str, *args: Any) -> str:
            answer = await super().execute(sql, *args)
            if sql == "COMMIT":
                try:
                    await asyncio.Future()
                finally:
                    self.executed.append("WORK ENDED")
            return answer

    conn = Stalled()
    owner = cell_control.Owner()
    owner.conn = conn
    monkeypatch.setattr(cell_control, "TRANSACTION_S", 0.04)
    with pytest.raises(TimeoutError):
        await owner.transaction(AsyncMock())
    assert conn.executed.index("CLOSE") < conn.executed.index("WORK ENDED")
    assert "TERMINATE" not in conn.executed and "ROLLBACK" not in conn.executed
    assert any("pg_logical_emit_message" in sql for sql in conn.executed)


@pytest.mark.asyncio
async def test_failure_never_starts_rollback_or_replaces_original():
    conn = FakeConn()
    owner = cell_control.Owner()
    owner.conn = conn
    original = ValueError("original failure")
    with pytest.raises(ValueError) as found:
        await owner.transaction(AsyncMock(side_effect=original))
    assert found.value is original
    assert conn.executed[-1] == "CLOSE"
    assert "ROLLBACK" not in conn.executed and "TERMINATE" not in conn.executed


@pytest.mark.asyncio
async def test_cancellation_during_body_closes_before_work_cancel():
    conn = FakeConn()
    owner = cell_control.Owner()
    owner.conn = conn
    entered = asyncio.Event()

    async def body():
        entered.set()
        await asyncio.Future()

    task = asyncio.create_task(owner.transaction(body))
    try:
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel("original cancellation")
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            done, _ = await asyncio.wait({task}, timeout=0.5)
            if not done:
                raise TimeoutError("owned test task did not finish")
            await task
        assert conn.executed[-1] == "CLOSE" and "ROLLBACK" not in conn.executed
    except TimeoutError:
        if task.done():
            task.result()
        raise
    finally:
        owned_tasks = {task, *owner._tasks}
        if owner._close_task is not None:
            owned_tasks.add(owner._close_task)
        for child in owned_tasks:
            if not child.done():
                child.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)


def snapshot():
    return {
        "cell": "first",
        "is_first": True,
        "host": "first.example",
        "deployment_id": "dep",
        "bucket": "",
        "bucket_prefix": "",
        "probe_url": "",
        "upstream_url": "",
        "current_fp": None,
        "next_fp": None,
        "mutation_generation": str(UUID(int=1)),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("after_claim", [False, True])
async def test_lost_lock_never_reconnects_or_continues_claim(monkeypatch, after_claim):
    lock = AsyncMock(
        side_effect=[None, cell_control._refused("lost")]
        if after_claim
        else cell_control._refused("lost")
    )
    monkeypatch.setattr(cell_control, "check_lock", lock)
    monkeypatch.setattr(cell_control, "paired", AsyncMock())
    call = AsyncMock(return_value={**snapshot(), "mutation_generation": str(UUID(int=2))})
    with pytest.raises(cell_control.Refused, match="lost"):
        await cell_control.claim(object(), call, snapshot())
    assert call.await_count == int(after_claim)


@pytest.mark.asyncio
async def test_unknown_claim_acknowledgement_never_reuses_the_observed_generation(monkeypatch):
    monkeypatch.setattr(cell_control, "check_lock", AsyncMock())
    monkeypatch.setattr(cell_control, "paired", AsyncMock())
    call = AsyncMock(side_effect=OSError("reply lost"))
    with pytest.raises(OSError, match="reply lost"):
        await cell_control.claim(object(), call, snapshot())
    assert call.await_count == 1
    assert "mutation_generation" not in call.call_args.args[1]["expected"]


def test_older_router_snapshot_cannot_make_a_control_request():
    row = snapshot()
    row.pop("mutation_generation")
    with pytest.raises(cell_control.Refused, match="not upgraded"):
        cell_control.preconditions(row)


@pytest.mark.asyncio
async def test_whole_router_request_is_bounded_even_without_a_transport_reply(monkeypatch):
    import httpx

    async def stalled(_request):
        await asyncio.Future()

    monkeypatch.setattr(cell_control, "REQUEST_S", 0.01)
    with pytest.raises(cell_control.Refused, match="reply unknown.*TimeoutError"):
        await cell_control.request(
            httpx.URL("http://127.0.0.1:1"),
            "first",
            httpx.MockTransport(stalled),
            "/claim",
            {},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["55P03", "57014"])
@pytest.mark.parametrize("where", ["UPDATE loom.tenancy_mode", "COMMIT"])
async def test_mode_contention_is_busy_only_before_commit_after_acknowledged_rollback(
    sqlstate, where
):
    from server.cli import cell_attach_pg as apg
    from tests.unit.cell_control_owner import owner
    from tests.unit.test_cell_attach import CURRENT, NEXT

    class Busy(Exception):
        pass

    busy = Busy("expected contention")
    busy.sqlstate = sqlstate
    routes = [(where, busy)]
    if where == "COMMIT":
        routes.append(("UPDATE loom.tenancy_mode", "UPDATE 1"))
    conn = FakeConn(routes)
    owned = owner(conn)
    marks = []
    if where == "COMMIT":
        with pytest.raises(Busy) as found:
            await apg._mode_try(owned, 100, marks.append, "first", (CURRENT, NEXT))
        assert found.value is busy
        assert "ROLLBACK" not in conn.executed and marks == ["unknown"]
        assert owned.closed
    else:
        assert not await apg._mode_try(owned, 100, marks.append, "first", (CURRENT, NEXT))
        assert conn.executed[-1] == "ROLLBACK" and marks == ["unknown", "recording"]
        assert not owned.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["rollback", "ownership", "update-status"])
async def test_terminal_mode_failure_never_resets_unknown_or_retries(failed):
    import asyncpg

    from server.cli import cell_attach_pg as apg
    from tests.unit.cell_control_owner import owner
    from tests.unit.test_cell_attach import CURRENT, NEXT, _seq

    original = ValueError("rollback failed")
    routes = [("UPDATE loom.tenancy_mode", "UPDATE 0")]
    if failed != "update-status":
        routes = [("UPDATE loom.tenancy_mode", asyncpg.LockNotAvailableError("busy"))]
    if failed == "rollback":
        routes.append(("ROLLBACK", original))
    if failed == "ownership":
        routes.append((apg._HELD, _seq(True, False)))
    conn, marks = FakeConn(routes), []
    with pytest.raises(ValueError if failed == "rollback" else cell_control.Refused) as found:
        await apg.commit_enforced(
            owner(conn),
            AsyncMock(),
            AsyncMock(return_value=[]),
            apg.Wait(0),
            lambda _: None,
            marks.append,
            expected_cell="first",
            expected_fp=(CURRENT, NEXT),
        )
    if failed == "rollback":
        assert found.value is original
    assert marks == ["unknown"] and conn.executed[-1] == "CLOSE"
    assert conn.executed.count("BEGIN") == 1


@pytest.mark.asyncio
async def test_expired_phase_does_not_receive_another_exit_allowance(monkeypatch):
    import time

    class StalledClose(FakeConn):
        async def close(self, **kwargs):
            self.timeouts = [kwargs["timeout"]]
            self.executed.append("CLOSE START")
            await asyncio.Future()

    conn = StalledClose()

    async def opening(self, _what):
        self.conn = conn

    monkeypatch.setattr(cell_control.Owner, "_open", opening)
    monkeypatch.setattr(cell_control, "TRANSACTION_S", 0.04)

    async def body(owned):
        return await owned.transaction(lambda: asyncio.Future())

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await cell_control.run("test", body)
    assert time.monotonic() - started < 0.3
    assert conn.executed.count("CLOSE START") == 1
    assert 0 < conn.timeouts[0] <= 0.02
    assert "TERMINATE" in conn.executed


@pytest.mark.asyncio
async def test_late_connection_is_discarded_without_starting_command_work(monkeypatch):
    import asyncpg

    from server.core import pg
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_dsn", "postgres://not-connected")
    monkeypatch.setattr(pg, "_expected", None)

    connected, discarded = asyncio.Event(), asyncio.Event()

    class Late(FakeConn):
        def terminate(self):
            super().terminate()
            discarded.set()

    conn = Late()

    async def connect(*args, **kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            while not connected.is_set():
                try:
                    await connected.wait()
                except asyncio.CancelledError:
                    pass
        return conn

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(cell_control, "OPEN_S", 0.04)
    body = AsyncMock()
    with pytest.raises(TimeoutError):
        await cell_control.run("late", body)
    connected.set()
    await asyncio.wait_for(discarded.wait(), 0.3)
    body.assert_not_awaited()
    assert conn.executed == ["TERMINATE"]


@pytest.mark.asyncio
async def test_successful_body_cannot_hide_failed_required_close(monkeypatch):
    class Broken(FakeConn):
        async def close(self, **_):
            raise OSError("required close failed")

    conn = Broken()

    async def opening(self, _what):
        self.conn = conn

    monkeypatch.setattr(cell_control.Owner, "_open", opening)
    with pytest.raises(OSError, match="required close failed"):
        await cell_control.run("test", AsyncMock(return_value=7))
    assert "TERMINATE" in conn.executed


@pytest.mark.asyncio
async def test_late_connection_arriving_inside_reserve_is_closed_once(monkeypatch):
    import asyncpg

    from server.core import pg
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_dsn", "postgres://not-connected")
    monkeypatch.setattr(pg, "_expected", None)

    conn = FakeConn()

    async def connect(*args, **kwargs):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return conn

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setattr(cell_control, "OPEN_S", 0.08)
    work = AsyncMock()
    with pytest.raises(TimeoutError):
        await cell_control.run("late", work)
    work.assert_not_awaited()
    assert conn.executed == ["CLOSE"]
