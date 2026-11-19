"""Attach owners close active sessions before cancelling transaction work."""

import asyncio
import time

import pytest

from server.cli import cell_attach_pg as apg
from server.cli import cell_control
from server.core import cell_triggers, pg
from tests.integration.test_pg_owned_cleanup import database as database
from tests.integration.test_pg_owned_cleanup import released, sleeping
from tests.unit.cell_control_owner import owner as _owner

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(
    "owner,phase",
    [
        ("trigger", "query"),
        ("trigger", "rollback"),
        ("mode", "query"),
        ("mode", "rollback"),
        ("direct", "query"),
    ],
)
@pytest.mark.parametrize("interruption", ["cancel", "deadline", "second-cancel"])
async def test_attach_owner_releases_backend_locks_before_return(
    database, monkeypatch, owner, phase, interruption
):
    import os

    import asyncpg

    from server.settings import settings
    from tests.integration._pg_database import with_database

    conn, observer = database
    dsn = with_database(
        os.environ["LOOM_PG_DSN"], await observer.fetchval("SELECT current_database()")
    )
    monkeypatch.setattr(settings, "pg_dsn", dsn)
    monkeypatch.setattr(pg, "_expected", None)
    await conn.execute(
        "CREATE SCHEMA loom; CREATE TABLE loom.tenancy_mode(mode text, since timestamptz); INSERT INTO loom.tenancy_mode VALUES('recording', now())"
    )
    pid = conn.get_server_pid()
    execute = asyncpg.Connection.execute
    closing = asyncio.Event()
    close = asyncpg.Connection.close

    async def intercepted(self, query, *args, **kwargs):
        if self is conn:
            if query.startswith("SET LOCAL statement_timeout"):
                return await execute(self, "SET LOCAL statement_timeout = '10000ms'")
            if query == "RAISE Python error":
                raise asyncpg.LockNotAvailableError("trigger statement busy")
            if phase == "rollback" and query.startswith("ROLLBACK"):
                await execute(self, "SELECT pg_sleep(10)")
            if owner == "mode" and query.startswith("LOCK TABLE"):
                await execute(self, "SELECT * FROM probe FOR UPDATE")
                if phase == "rollback":
                    raise asyncpg.LockNotAvailableError("mode statement busy")
                return await execute(self, "SELECT pg_sleep(10)")
            if owner == "direct" and query.startswith("BEGIN"):
                await execute(self, query)
                return await execute(self, "SELECT * FROM probe FOR UPDATE; SELECT pg_sleep(10)")
        return await execute(self, query, *args, **kwargs)

    async def closing_session(self, *args, **kwargs):
        was_open = not self.is_closed()
        await close(self, *args, **kwargs)
        if self is conn and was_open and interruption == "second-cancel":
            closing.set()
            await asyncio.Future()

    async def ensure(conn, table, **kwargs):
        statements = ["SELECT * FROM probe FOR UPDATE"]
        statements += ["RAISE Python error"] if phase == "rollback" else ["SELECT pg_sleep(10)"]
        return await cell_triggers._run_ddl(
            conn, table, statements, kwargs["lock_timeout"], 1, warn=False
        )

    async def connect(*args, **kwargs):
        return conn

    monkeypatch.setattr(asyncpg.Connection, "execute", intercepted)
    monkeypatch.setattr(asyncpg.Connection, "close", closing_session)
    monkeypatch.setattr(cell_triggers, "ensure_table", ensure)
    monkeypatch.setattr(apg, "_TRIGGER_TRY_MARGIN_S", 0.4)
    monkeypatch.setattr(apg, "_MODE_STANDBY_S", 0.5)
    monkeypatch.setattr(apg, "_MODE_STATEMENT_MARGIN_MS", 0)
    monkeypatch.setattr(cell_control, "OPEN_S", 0.6)
    monkeypatch.setattr(asyncpg, "connect", connect)
    await apg.take_lock(conn, "cleanup test")
    if owner == "trigger":
        work = apg._ensure_one(_owner(conn), "probe", 100, apg.Wait(0))
    elif owner == "mode":
        work = apg._mode_try(_owner(conn), 100, lambda _: None, "first", (None, None))
    else:

        async def body(_owner):
            raise AssertionError("direct checks unexpectedly completed")

        work = cell_control.run("test", body)
    task = asyncio.create_task(work)
    try:
        await sleeping(observer, pid)
        started = time.monotonic()
        if interruption != "deadline":
            task.cancel()
        if interruption == "second-cancel":
            await asyncio.wait_for(closing.wait(), 1)
            task.cancel()
        with pytest.raises(TimeoutError if interruption == "deadline" else asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 1.2)
        assert time.monotonic() - started < 0.7
        await released(observer, pid)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unexpected_trigger_error_reaches_owner_before_rollback(database, monkeypatch):
    import asyncpg

    from server.core import cell

    conn, observer = database
    pid = conn.get_server_pid()
    await conn.execute("SELECT pg_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
    execute = asyncpg.Connection.execute
    original = ValueError("unexpected trigger operation failed")

    async def intercepted(self, query, *args, **kwargs):
        if self is conn:
            if query == "RAISE unexpected Python error":
                assert await observer.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1 AND locktype='advisory')",
                    pid,
                )
                raise original
            if query.startswith("ROLLBACK"):
                await execute(self, "SELECT pg_sleep(10)")
        return await execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", intercepted)
    started = time.monotonic()
    with pytest.raises(ValueError) as error:
        await _owner(conn).call(
            lambda: cell_triggers._run_ddl(
                conn,
                "probe",
                ["SELECT * FROM probe FOR UPDATE", "RAISE unexpected Python error"],
                attempts=1,
                warn=False,
            ),
            total_s=0.5,
        )
    assert error.value is original
    assert time.monotonic() - started < 0.5
    await released(observer, pid)
