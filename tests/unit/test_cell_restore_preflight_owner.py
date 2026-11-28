"""The public restore deadline owns the org-free state read and its cleanup."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import textwrap
import weakref
from uuid import UUID

import asyncpg
import pytest

from server.cli import cell_restore
from server.core import cell, ch, pg, provider_process, retirement
from server.settings import settings


class ClickHouse:
    def __init__(self, events):
        self.events = events
        self.connected = False
        self.authority_reads = 0

    async def connect(self):
        self.connected = True
        self.events.append("clickhouse connected")

    async def query_one(self, sql):
        assert sql == (
            "SELECT authority, epoch, deployment_id FROM loom.storage_authority FINAL "
            "SETTINGS select_sequential_consistency = 1"
        )
        self.authority_reads += 1
        return {"authority": "postgres", "epoch": 7, "deployment_id": "restore-deployment"}

    async def disconnect(self):
        self.connected = False
        self.events.append("clickhouse disconnected")


class Connection:
    def __init__(self, failure):
        self.failure = failure
        self.events = []
        self.connects = 0
        self.deployment_reads = 0
        self.codecs = []
        self.state_reads = 0
        self.close_timeouts = []
        self.closed = False
        self.terminations = 0
        self.state_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.release_close = None
        self.close_cancelled = False

    async def fetchrow(self, sql):
        assert sql == "SELECT epoch, deployment_id FROM loom.authority"
        self.deployment_reads += 1
        return {"epoch": 7, "deployment_id": "restore-deployment"}

    async def set_type_codec(self, typ, **kwargs):
        assert kwargs["schema"] == "pg_catalog"
        self.codecs.append(typ)

    async def fetchval(self, sql, tables):
        assert sql == "SELECT count(to_regclass(t)) FROM unnest($1::text[]) AS t"
        assert tables == list(cell._STATE_TABLES)
        self.state_reads += 1
        self.events.append("state read started")
        self.state_started.set()
        if self.failure is not None:
            self.events.append("state read failed")
            raise self.failure
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.events.append("state read cancelled")
            raise

    async def close(self, *, timeout):  # noqa: ASYNC109 — matches the driver close API.
        self.close_timeouts.append(timeout)
        self.events.append("public close started")
        self.close_started.set()
        try:
            if self.release_close is not None:
                await self.release_close.wait()
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.close_cancelled = True
            raise
        self.closed = True
        self.events.append("public close acknowledged")

    def terminate(self):
        self.terminations += 1

    def is_closed(self):
        return self.closed or bool(self.terminations)


def install_preflight(monkeypatch, *, failure=None):
    conn = Connection(failure)
    client = ClickHouse(conn.events)
    gate = retirement.RetirementGate()
    monkeypatch.setattr(retirement, "_gate", gate)
    monkeypatch.setattr(retirement, "_pid", os.getpid())
    monkeypatch.setattr(pg, "_state_pid", os.getpid())
    monkeypatch.setattr(pg, "_expected", None)
    for name in ("_pools", "_opening", "_closing"):
        monkeypatch.setattr(pg, name, weakref.WeakKeyDictionary())
    monkeypatch.setattr(provider_process, "_identity", (os.getpid(), UUID(int=796)))
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://unused/restore")
    monkeypatch.setattr(settings, "pg_statement_timeout_ms", 5000)
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "router_operator_token", "test-operator")
    monkeypatch.setattr(ch, "get_client", lambda: client)

    async def connect(dsn, **kwargs):
        assert dsn == "postgresql://unused/restore"
        assert kwargs["server_settings"]["loom.tenancy_checked"] == "on"
        conn.connects += 1
        return conn

    monkeypatch.setattr(asyncpg, "connect", connect)
    return conn, client, gate


def args(wait):
    return argparse.Namespace(cell="first", router="https://router.test", wait=wait)


@pytest.mark.parametrize("wait", [0.2, 0.8])
def test_public_preflight_deadline_closes_owned_state_read_before_cancellation(
    monkeypatch, capsys, wait
):
    conn, client, gate = install_preflight(monkeypatch)
    assert cell_restore.cmd_restore(args(wait)) == 1
    assert conn.connects == 1 and conn.state_reads == 1
    assert client.authority_reads == 1 and conn.deployment_reads == 1
    assert conn.codecs == ["json", "jsonb"]
    assert conn.events.index("public close acknowledged") < conn.events.index(
        "state read cancelled"
    )
    assert len(conn.close_timeouts) == 1 and 0 < conn.close_timeouts[0] <= wait
    assert conn.closed and conn.terminations == 0
    assert gate.active == 0 and not client.connected
    diagnostic = capsys.readouterr().err
    assert "outcome may be unknown" in diagnostic
    assert "rerun the same command before starting the cell" in diagnostic


def test_public_preflight_state_error_keeps_refusal_and_original_cleanup_budget(
    monkeypatch, capsys
):
    failure = ValueError("state read refused")
    conn, client, gate = install_preflight(monkeypatch, failure=failure)
    wait = 0.8
    assert cell_restore.cmd_restore(args(wait)) == 1
    assert conn.connects == 1 and conn.state_reads == 1
    assert client.authority_reads == 1 and conn.deployment_reads == 1
    assert conn.codecs == ["json", "jsonb"]
    assert conn.events.index("state read failed") < conn.events.index("public close acknowledged")
    assert len(conn.close_timeouts) == 1 and 0 < conn.close_timeouts[0] <= wait
    assert conn.closed and conn.terminations == 0
    assert gate.active == 0 and not client.connected
    diagnostic = capsys.readouterr().err
    assert "state read refused" in diagnostic
    assert "outcome may be unknown" in diagnostic


@pytest.mark.parametrize("kind", ["SystemExit", "KeyboardInterrupt"])
async def test_public_preflight_transports_identical_fatal_after_acknowledged_close(kind):
    script = textwrap.dedent(f"""
        import pytest
        from server.cli import cell_restore
        from tests.unit.test_cell_restore_preflight_owner import args, install_preflight

        fatal = {kind}('native state read failure')
        with pytest.MonkeyPatch.context() as patch:
            conn, client, gate = install_preflight(patch, failure=fatal)
            try:
                cell_restore.cmd_restore(args(0.8))
            except BaseException as caught:
                conn.events.append('fatal observed at public boundary')
                assert caught is fatal, (type(caught).__name__, str(caught))
                assert conn.connects == 1 and conn.state_reads == 1
                assert client.authority_reads == 1 and conn.deployment_reads == 1
                assert conn.codecs == ['json', 'jsonb']
                assert conn.closed and conn.terminations == 0
                assert len(conn.close_timeouts) == 1 and 0 < conn.close_timeouts[0] <= 0.8
                assert conn.events.index('state read failed') < conn.events.index(
                    'public close acknowledged'
                )
                assert conn.events.index('public close acknowledged') < conn.events.index(
                    'fatal observed at public boundary'
                )
                assert gate.active == 0 and not client.connected
                print('IDENTICAL_FATAL_AFTER_CLOSE', flush=True)
            else:
                raise AssertionError('the public boundary swallowed the fatal exception')
    """)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env={**os.environ, "LOOM_ENV_FILE": ""},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        assert stdout.splitlines() == [b"IDENTICAL_FATAL_AFTER_CLOSE"]
        assert b"never retrieved" not in stderr
        assert b"Task was destroyed" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.parametrize("second_cancellation", [False, True])
def test_public_preflight_cancellation_closes_before_cancelling_the_owned_read(
    monkeypatch, capsys, second_cancellation
):
    conn, client, gate = install_preflight(monkeypatch)
    original_run = asyncio.run
    wait = 1.0

    def supervised_run(work):
        async def supervise():
            conn.release_close = asyncio.Event()
            task = asyncio.create_task(work)
            try:
                await asyncio.wait_for(conn.state_started.wait(), 0.5)
                task.cancel("first cancellation")
                await asyncio.wait_for(conn.close_started.wait(), 0.5)
                if second_cancellation:
                    task.cancel("second cancellation")
                    await asyncio.sleep(0)
                    assert not conn.close_cancelled
                conn.release_close.set()
                try:
                    await asyncio.wait_for(task, 0.5)
                except asyncio.CancelledError as error:
                    return error
                raise AssertionError("the public operation swallowed cancellation")
            finally:
                conn.release_close.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        # Return from the supervisor before raising, preserving the captured payload.
        error = original_run(supervise())
        raise error

    monkeypatch.setattr(cell_restore.asyncio, "run", supervised_run)
    with pytest.raises(asyncio.CancelledError, match="first cancellation"):
        cell_restore.cmd_restore(args(wait))
    assert conn.connects == 1 and conn.state_reads == 1
    assert client.authority_reads == 1 and conn.deployment_reads == 1
    assert conn.codecs == ["json", "jsonb"]
    assert conn.events.index("public close acknowledged") < conn.events.index(
        "state read cancelled"
    )
    assert len(conn.close_timeouts) == 1 and 0 < conn.close_timeouts[0] <= wait
    assert conn.closed and conn.terminations == 0 and not conn.close_cancelled
    assert gate.active == 0 and not client.connected
    diagnostic = capsys.readouterr().err
    assert "outcome may be unknown" in diagnostic
    assert "rerun the same command before starting the cell" in diagnostic


def test_public_preflight_owned_read_timeout_uses_a_fixed_deadline_diagnostic(monkeypatch, capsys):
    private = "postgresql://private-user:private-secret@private-host/database"
    conn, client, gate = install_preflight(monkeypatch, failure=TimeoutError(private))
    wait = 0.8
    assert cell_restore.cmd_restore(args(wait)) == 1
    assert conn.connects == 1 and conn.state_reads == 1
    assert conn.closed and conn.terminations == 0
    assert len(conn.close_timeouts) == 1 and 0 < conn.close_timeouts[0] <= wait
    assert gate.active == 0 and not client.connected
    diagnostic = capsys.readouterr().err
    assert "cell state read reached its deadline" in diagnostic
    assert private not in diagnostic and "no answer within 10 s" not in diagnostic
    assert "outcome may be unknown" in diagnostic
