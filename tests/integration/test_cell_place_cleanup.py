"""Placement cleanup observes the actual marked backend through interrupted work."""

from __future__ import annotations

import asyncio
import contextlib
import time

import asyncpg
import pytest

from server.cli import cell_place
from server.core import pg
from tests.integration import test_cell_place as placement


@pytest.fixture
async def stores(monkeypatch):
    async for value in placement.stores.__wrapped__(monkeypatch):
        yield value


async def waiting(observer, pid):
    async with asyncio.timeout(3):
        while True:
            if await observer.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1 "
                "AND wait_event='PgSleep')",
                pid,
            ):
                return
            await asyncio.sleep(0.01)


async def assert_released(observer, pid):
    assert not await observer.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=$1)", pid)
    assert not await observer.fetchval(
        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid=$1)", pid
    )


@pytest.mark.parametrize("interrupt", ["deadline", "cancel", "second_cancel"])
async def test_placement_owner_closes_before_cancelling_actual_rollback(
    stores, monkeypatch, interrupt
):
    # Only the exact ROLLBACK wire operation is delayed. The transaction context,
    # sleeping query, session lock, observer and close are the installed real driver.
    original_identity, execute = cell_place._identity, asyncpg.Connection.execute
    known = asyncio.Event()
    captured = {}
    queries = []

    async def stalled_rollback(conn, sql, *args, **kwargs):
        if conn is captured.get("conn") and sql == "ROLLBACK;":
            query = asyncio.create_task(execute(conn, "SELECT pg_sleep(10)"))
            queries.append(query)
            while not query.done():
                try:
                    await asyncio.shield(query)
                except asyncio.CancelledError:
                    continue
            query.result()
        return await execute(conn, sql, *args, **kwargs)

    async def identity(conn, name):
        await original_identity(conn, name)
        captured["conn"] = conn
        known.set()
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(193789)")
            raise ValueError("placement body failed before rollback")

    monkeypatch.setattr(cell_place, "_identity", identity)
    monkeypatch.setattr(cell_place, "_bound_s", lambda: 0.5)
    monkeypatch.setattr(asyncpg.Connection, "execute", stalled_rollback)
    observer = await asyncpg.connect(stores["first"])
    task = asyncio.create_task(placement._local(stores, monkeypatch, "first"))
    try:
        await asyncio.wait_for(known.wait(), 3)
        conn = captured["conn"]
        pid = conn.get_server_pid()
        await waiting(observer, pid)
        started = time.monotonic()
        if interrupt != "deadline":
            task.cancel()
            if interrupt == "second_cancel":
                # A second signal arrives while the same operation is still active.
                await asyncio.sleep(0)
                task.cancel()
        done, _ = await asyncio.wait({task}, timeout=0.8)
        assert done, "placement must close its backend before waiting for rollback unwind"
        with pytest.raises(TimeoutError if interrupt == "deadline" else asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 0.8
        await assert_released(observer, pid)
    finally:
        # Exact owned test resources only; even a deliberately failing old owner
        # cannot leave its query or scratch database alive after the regression.
        if "conn" in captured and not captured["conn"].is_closed():
            with contextlib.suppress(Exception):
                await captured["conn"].close(timeout=1)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, *queries, return_exceptions=True)
        await observer.close(timeout=1)
        pg.expect_deployment(None)
