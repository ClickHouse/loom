"""Restore keeps its deadline across driver transaction cleanup and validates replies."""

from __future__ import annotations

import argparse
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from asyncpg.transaction import Transaction

from server.cli import cell_restore
from server.cli import cell_restore_pg as local
from server.cli import cell_restore_stores as stores
from server.core import cell
from server.router import restore as protocol
from server.settings import settings
from tests.unit.test_cell_restore_protocol import descriptor


class DriverConnection:
    _pool_release_ctr = 0
    _top_xact = None
    _protocol = SimpleNamespace(is_in_transaction=lambda: False)

    def __init__(self):
        self.closed = False
        self.terminated = asyncio.Event()
        self.rollback_started = asyncio.Event()
        self.statement_started = asyncio.Event()

    def is_closed(self):
        return self.terminated.is_set()

    def transaction(self):
        return Transaction(self, None, False, False)

    async def close(self, **kwargs):
        assert kwargs["timeout"] > 0
        self.closed = True
        self.terminated.set()

    def terminate(self):
        self.terminated.set()

    async def execute(self, sql):
        if sql == "ROLLBACK;":
            self.rollback_started.set()
        if sql == "BEGIN;":
            return "BEGIN"
        self.statement_started.set()
        await self.terminated.wait()
        raise ConnectionError("connection terminated")


async def test_installed_asyncpg_rollback_is_closed_before_supervisor_cancels():
    conn = DriverConnection()
    budget = cell_restore.Budget(0.08)
    budget.conn = conn

    async def work():
        async with asyncio.timeout(0.01), conn.transaction():
            await conn.execute("SELECT stalled")

    with pytest.raises(TimeoutError):
        await budget.call(work())
    assert conn.rollback_started.is_set() and conn.is_closed() and conn.closed


async def test_cancelled_supervisor_closes_owned_connection_before_reaping():
    conn = DriverConnection()
    budget = cell_restore.Budget(1)
    budget.conn = conn

    async def work():
        async with conn.transaction():
            await conn.execute("SELECT stalled")

    task = asyncio.create_task(budget.call(work()))
    try:
        try:
            await asyncio.wait_for(conn.statement_started.wait(), 0.5)
        except TimeoutError:
            if task.done():
                await task
            raise
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.5)
        assert conn.is_closed() and conn.closed
    finally:
        task.cancel()
        conn.terminate()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "local_mode,directory_mode,first,expected",
    [
        (None, "recording", True, "recording"),
        ("enforced", "recording", True, "enforced"),
        ("recording", "enforced", True, "enforced"),
        (None, "enforced", False, "enforced"),
    ],
)
def test_mode_only_rises(local_mode, directory_mode, first, expected):
    assert (
        local.target_mode(local_mode, {**descriptor(), "mode": directory_mode, "is_first": first})
        == expected
    )


@pytest.mark.parametrize("wait", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_refuses_before_connect(monkeypatch, wait):
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgres://not-connected")
    assert cell_restore._args_problem(argparse.Namespace(cell="first", wait=wait))


async def _http(monkeypatch, status, answer):
    async def respond(request):
        assert request.headers["authorization"] == f"Bearer {settings.router_operator_token}"
        return httpx.Response(status, json=answer)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(respond), **kw)
    )


async def test_changed_count_restarts_but_changed_configuration_refuses(monkeypatch):
    old = descriptor()
    new = {**old, "placed": 4}
    await _http(monkeypatch, 409, {"descriptor": new, "sha256": protocol.digest(new)})
    with pytest.raises(protocol.SnapshotChanged):
        await cell_restore._page(
            httpx.URL("https://router.test"), "first", "dep-first", "b", old, 1
        )


async def test_configuration_changed_reply_cannot_restart(monkeypatch):
    old = descriptor()
    new = {
        **old,
        "bucket": "different",
        "current_fp": cell.as_value({"deployment_id": "dep-first", "bucket": "different"}),
    }
    await _http(monkeypatch, 409, {"descriptor": new, "sha256": protocol.digest(new)})
    with pytest.raises(ValueError) as error:
        await cell_restore._page(
            httpx.URL("https://router.test"), "first", "dep-first", "b", old, 1
        )
    assert not isinstance(error.value, protocol.SnapshotChanged)


async def test_oversize_http_response_refuses_before_decoding(monkeypatch):
    await _http(monkeypatch, 200, {"padding": "x" * protocol.MAX_RESPONSE_BYTES})
    with pytest.raises(ValueError, match="byte bound"):
        await cell_restore._page(
            httpx.URL("https://router.test"), "first", "dep-first", None, None, 1
        )


def test_directory_fingerprint_uses_full_configured_org_reader(monkeypatch):
    from server.code.domain.index import repos

    monkeypatch.setattr(cell, "_code_orgs", None)
    monkeypatch.setattr(repos, "operator_orgs", lambda: {"repo-only"})
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    value = descriptor()
    value["current_fp"] = cell.as_value(
        {**cell.fingerprint("dep-first"), "configured_orgs": ["repo-only"]}
    )
    stores.descriptor(value, {"deployment_id": "dep-first"})
    assert "repo-only" in cell.fingerprint("dep-first")["configured_orgs"]


@pytest.mark.parametrize("disconnect_failure", [False, True])
def test_cli_cancellation_names_retry_and_preserves_cancellation(
    monkeypatch, capsys, disconnect_failure
):
    from server.core import ch

    cancelled = asyncio.CancelledError("lost commit response")
    monkeypatch.setattr(cell_restore, "_args_problem", lambda args: "")
    monkeypatch.setattr(
        cell_restore.cell_cli, "operator_router", lambda args: httpx.URL("https://router.test")
    )
    monkeypatch.setattr(cell_restore, "connect_declared", AsyncMock())
    monkeypatch.setattr(
        cell_restore,
        "disconnect",
        AsyncMock(side_effect=RuntimeError("disconnect failed") if disconnect_failure else None),
    )
    monkeypatch.setattr(ch, "get_client", lambda: object())
    monkeypatch.setattr(cell_restore.Restore, "run", AsyncMock(side_effect=cancelled))
    with pytest.raises(asyncio.CancelledError) as error:
        cell_restore.cmd_restore(argparse.Namespace(cell="first", wait=1))
    assert error.value is cancelled
    printed = capsys.readouterr()
    assert printed.out == "" and "rerun the same command" in printed.err


async def test_second_cancellation_during_public_close_preserves_first_cancellation():
    started, release, cancelled_close = asyncio.Event(), asyncio.Event(), asyncio.Event()
    conn = DriverConnection()

    async def stalled_close(**kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled_close.set()
            raise
        conn.closed = True
        conn.terminated.set()

    conn.close = stalled_close
    budget = cell_restore.Budget(1)
    budget.conn = conn
    task = asyncio.create_task(budget.call(conn.execute("SELECT stalled")))
    try:
        try:
            await asyncio.wait_for(conn.statement_started.wait(), 0.5)
        except TimeoutError:
            if task.done():
                await task
            raise
        task.cancel("first cancellation")
        await asyncio.wait_for(started.wait(), 1)
        task.cancel("second cancellation")
        await asyncio.sleep(0)
        assert not cancelled_close.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await asyncio.wait_for(task, 0.5)
        assert conn.is_closed() and conn.closed and not cancelled_close.is_set()
    finally:
        release.set()
        task.cancel()
        conn.terminate()
        await asyncio.gather(task, return_exceptions=True)


async def test_budget_keeps_original_failure_when_owned_close_fails():
    conn = DriverConnection()
    budget = cell_restore.Budget(1)
    budget.conn = conn

    async def broken_close(**kwargs):
        raise RuntimeError("cleanup failure")

    async def failing_work():
        raise ValueError("operation failure")

    conn.close = broken_close
    with pytest.raises(ValueError, match="operation failure"):
        await budget.call(failing_work())
    assert conn.is_closed()


def test_retained_configuration_mismatch_names_recovery_without_changing_prepare(
    monkeypatch, capsys
):
    import json

    from server.core import ch

    monkeypatch.setattr(cell_restore, "_args_problem", lambda args: "")
    monkeypatch.setattr(
        cell_restore.cell_cli, "operator_router", lambda args: httpx.URL("https://router.test")
    )
    monkeypatch.setattr(cell_restore, "_connect", AsyncMock())
    monkeypatch.setattr(cell_restore, "disconnect", AsyncMock())
    monkeypatch.setattr(ch, "get_client", lambda: object())
    monkeypatch.setattr(
        cell_restore.Restore,
        "run",
        AsyncMock(
            return_value={"prepare_required": False, "configuration_recovery_required": True}
        ),
    )
    assert cell_restore.cmd_restore(argparse.Namespace(cell="first", wait=1)) == 0
    printed = capsys.readouterr()
    assert json.loads(printed.out)["prepare_required"] is False
    assert "restore a matching configuration" in printed.err
    assert "before starting processes" in printed.err


async def test_closed_restore_attempt_refuses_before_raw_acquisition(monkeypatch):
    from server.cli import cell_control
    from server.core import pg

    connect = AsyncMock(return_value=DriverConnection())
    monkeypatch.setattr(pg, "_connect_dedicated", connect)
    budget = cell_restore.Budget(1)
    budget.closed = True
    runner = cell_restore.Restore(
        argparse.Namespace(cell="first"), httpx.URL("http://127.0.0.1:7901"), budget
    )
    with pytest.raises(cell_control.Refused):
        await runner.run(object())
    connect.assert_not_awaited()


@pytest.mark.parametrize("encoding", ["LATIN1", "SQL_ASCII", "UTF8"])
async def test_restore_refuses_unsupported_encoding_before_control_writes(monkeypatch, encoding):
    import asyncpg

    from server.cli import cell_attach_pg as apg
    from server.cli import cell_restore_stores
    from server.core import pg

    monkeypatch.setattr(settings, "pg_dsn", "postgres://not-connected")
    monkeypatch.setattr(pg, "_expected", None)
    conn = DriverConnection()
    conn.fetchval = AsyncMock(return_value=encoding)
    monkeypatch.setattr(asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(pg, "_check_deployment", AsyncMock())
    monkeypatch.setattr(pg, "init_connection", AsyncMock())
    monkeypatch.setattr(apg, "direct_refusals", AsyncMock(return_value=[]))
    lock = AsyncMock(side_effect=ValueError("healthy encoding reached the lock boundary"))
    monkeypatch.setattr(apg, "take_lock", lock)
    later = []
    for module, name in (
        (cell_restore_stores, "identity"),
        (cell_restore_stores, "stopped_and_schema"),
        (cell_restore_stores, "nonces"),
        (cell_restore, "_migrate"),
    ):
        callback = AsyncMock()
        monkeypatch.setattr(module, name, callback)
        later.append(callback)
    runner = cell_restore.Restore(
        argparse.Namespace(cell="first"), httpx.URL("http://127.0.0.1:7901"), cell_restore.Budget(5)
    )
    for name in ("control", "finish"):
        callback = AsyncMock()
        monkeypatch.setattr(runner, name, callback)
        later.append(callback)
    expected = "healthy encoding reached the lock boundary" if encoding == "UTF8" else "not UTF8"
    with pytest.raises(ValueError, match=expected):
        await runner.run(object())
    if encoding == "UTF8":
        lock.assert_awaited_once_with(conn, "restore")
    else:
        lock.assert_not_awaited()
        conn.fetchval.assert_awaited_once_with(
            "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database()"
        )
    for callback in later:
        callback.assert_not_awaited()
    assert runner.budget.conn is conn
    assert conn.is_closed() and conn.closed
