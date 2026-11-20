"""Cancellation of owned Postgres work releases its actual backend locks."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from server.core import pg
from server.core.schema import pg_migrate
from tests.integration._pg_database import create_database, drop_database

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def database() -> AsyncIterator[tuple[Any, Any]]:
    import asyncpg

    admin = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    name, dsn = await create_database(admin)
    async with contextlib.AsyncExitStack() as cleanup:
        try:
            cleanup.push_async_callback(drop_database, admin, name)
            conn = await asyncpg.connect(dsn, server_settings={"statement_timeout": "10000"})
            cleanup.push_async_callback(conn.close, timeout=1)
            observer = await asyncpg.connect(dsn)
            cleanup.push_async_callback(observer.close, timeout=1)
            await conn.execute(
                "CREATE TABLE probe(id int PRIMARY KEY); INSERT INTO probe VALUES(1)"
            )
            yield conn, observer
        except BaseException:  # Owned cleanup cannot replace the first failure.
            with contextlib.suppress(BaseException):
                await cleanup.aclose()
            raise


async def sleeping(observer: Any, pid: int) -> None:
    async with asyncio.timeout(2):
        for _ in range(400):
            if await observer.fetchval(
                "SELECT wait_event='PgSleep' FROM pg_stat_activity WHERE pid=$1", pid
            ):
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("backend did not reach pg_sleep")
    assert await observer.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", pid)


async def released(observer: Any, pid: int) -> None:
    assert not await observer.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", pid)
    assert not await observer.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
    )


async def test_owned_migrator_cancellation_releases_server_locks(database, tmp_path: Path) -> None:
    conn, observer = database
    (tmp_path / "001_sleep.sql").write_text("SELECT * FROM probe FOR UPDATE; SELECT pg_sleep(10);")

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        yield conn

    task = asyncio.create_task(pg_migrate.migrate(tmp_path, connect=connect, schema="test"))
    try:
        await sleeping(observer, conn.get_server_pid())
        pid = conn.get_server_pid()
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1.5)
        assert time.monotonic() - started < 1.5
        await released(observer, pid)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("signals", [1, 2])
async def test_owned_migrator_keeps_real_cancel_request_alive(
    database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signals: int
) -> None:
    from asyncpg import connect_utils

    conn, observer = database
    pid = conn.get_server_pid()
    cancelling, allow_cancel = asyncio.Event(), asyncio.Event()
    real_cancel = connect_utils._cancel
    monkeypatch.setattr(pg, "_RELEASE_TIMEOUT_S", 0.5)

    async def held_cancel(**kwargs: Any) -> None:
        if kwargs["backend_pid"] == pid:
            cancelling.set()
            await allow_cancel.wait()
        await real_cancel(**kwargs)

    monkeypatch.setattr(connect_utils, "_cancel", held_cancel)
    (tmp_path / "001_sleep.sql").write_text("SELECT * FROM probe FOR UPDATE; SELECT pg_sleep(10);")

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        yield conn

    task = asyncio.create_task(pg_migrate.migrate(tmp_path, connect=connect, schema="test"))
    try:
        await sleeping(observer, pid)
        task.cancel("first signal")
        await asyncio.wait_for(cancelling.wait(), 2)
        if signals == 2:
            task.cancel("second signal")
        await asyncio.sleep(0.03)
        assert not task.done(), (
            await observer.fetchval("SELECT wait_event FROM pg_stat_activity WHERE pid=$1", pid),
            await observer.fetchval("SELECT count(*) FROM pg_locks WHERE pid=$1", pid),
        )
        allow_cancel.set()
        with pytest.raises(asyncio.CancelledError, match="first signal"):
            await asyncio.wait_for(task, 0.5)
        await released(observer, pid)
    finally:
        allow_cancel.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await observer.execute("SELECT pg_cancel_backend($1)", pid)
        async with asyncio.timeout(2):
            for _ in range(200):
                if not await observer.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("owned backend did not end after teardown cancellation")


@pytest.mark.parametrize("transaction_context", [False, True])
async def test_public_owned_close_releases_live_query_before_task_cancel(
    database, transaction_context: bool
) -> None:
    conn, observer = database
    pid = conn.get_server_pid()

    async def work() -> None:
        if transaction_context:
            async with conn.transaction():
                await conn.execute("SELECT * FROM probe FOR UPDATE; SELECT pg_sleep(10)")
        else:
            await conn.execute("BEGIN; SELECT * FROM probe FOR UPDATE; SELECT pg_sleep(10)")

    task = asyncio.create_task(work())
    try:
        await sleeping(observer, pid)
        await pg.close_owned(conn, end=time.monotonic() + 0.5)
        await released(observer, pid)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_borrowed_migrator_leaves_failure_cleanup_to_owner(database, tmp_path: Path) -> None:
    conn, observer = database
    (tmp_path / "001_sleep.sql").write_text("SELECT * FROM probe FOR UPDATE; SELECT pg_sleep(10);")
    task = asyncio.create_task(pg_migrate.migrate_borrowed(conn, tmp_path, schema="test"))
    pid = conn.get_server_pid()
    try:
        await sleeping(observer, pid)
        await pg.close_owned(conn, end=time.monotonic() + 0.5)
        await released(observer, pid)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_borrowed_migrator_normal_completion_retains_owners_lock(database, tmp_path) -> None:
    conn, observer = database
    await conn.execute("SELECT pg_advisory_lock(712398)")
    (tmp_path / "001_write.sql").write_text("INSERT INTO probe VALUES(2)")
    assert await pg_migrate.migrate_borrowed(conn, tmp_path, schema="test") == ["001_write.sql"]
    assert not conn.is_closed()
    assert await observer.fetchval("SELECT count(*) FROM probe") == 2
    assert await conn.fetchval("SELECT pg_advisory_unlock(712398)")
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'"
        )
        == 0
    )


async def test_owned_close_interrupts_installed_transaction_rollback(database, monkeypatch) -> None:
    import asyncpg

    conn, observer = database
    pid = conn.get_server_pid()
    execute = asyncpg.Connection.execute

    async def stalled_rollback(self, query, *args, **kwargs):
        if self is conn and query.startswith("ROLLBACK"):
            await execute(self, "SELECT pg_sleep(10)")
        return await execute(self, query, *args, **kwargs)

    monkeypatch.setattr(asyncpg.Connection, "execute", stalled_rollback)

    async def work() -> None:
        async with conn.transaction():
            await conn.execute("SELECT * FROM probe FOR UPDATE")
            raise ValueError("statement failure before rollback")

    task = asyncio.create_task(work())
    try:
        await sleeping(observer, pid)
        await pg.close_owned(conn, end=time.monotonic() + 0.5)
        await released(observer, pid)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_borrowed_failure_keeps_the_session_for_its_owner(database, tmp_path) -> None:
    import asyncpg

    conn, _ = database
    (tmp_path / "001_failure.sql").write_text("SELECT * FROM missing_table_for_failure")
    with pytest.raises(asyncpg.UndefinedTableError):
        await pg_migrate.migrate_borrowed(conn, tmp_path, schema="test")
    assert not conn.is_closed()
    assert await conn.fetchval("SELECT pg_advisory_unlock($1)", pg_migrate._LOCK_KEY)
    assert await conn.fetchval("SELECT count(*) FROM test.pg_migrations") == 0
