"""The tenancy check's statements against a real Postgres (server/auth/tenancy.py): the
recording insert records an org only while the mode is `recording`, a second insert
conflicts, the placement insert is idempotent, attach's write of the mode and a
recording insert never cross, and `loom.cell_first` refuses an update, a delete or a
truncate, also under `session_replication_role = replica`. Each test leaves the
database as it found it."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import TYPE_CHECKING

import pytest

from server.auth import tenancy

if TYPE_CHECKING:
    import asyncpg


@pytest.mark.asyncio
async def test_the_recording_and_placement_statements() -> None:
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        if not await conn.fetchval("SELECT to_regclass('loom.tenancy') IS NOT NULL"):
            pytest.skip("this Postgres has not applied 008_cells.sql")
        tx = conn.transaction()
        await tx.start()
        try:
            a, b, c = (f"tenancy-it-{uuid.uuid4().hex[:8]}" for _ in range(3))
            await conn.execute("DELETE FROM loom.tenancy_mode")
            await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('recording')")
            assert await conn.fetchval(tenancy._RECORD, a) == 1
            assert await conn.fetchval(tenancy._RECORD, a) is None  # conflicts: recorded once
            assert await conn.fetchval(tenancy._HOLDS, a) == 1
            await conn.execute("UPDATE loom.tenancy_mode SET mode = 'enforced'")
            assert await conn.fetchval(tenancy._RECORD, b) is None  # enforced records nothing
            assert await conn.fetchval(tenancy._HOLDS, b) is None
            await conn.execute(tenancy._ADD, c)
            await conn.execute(tenancy._ADD, c)  # a retried placement writes no second row
            assert await conn.fetchval("SELECT count(*) FROM loom.tenancy WHERE org = $1", c) == 1
        finally:
            await tx.rollback()
    finally:
        await conn.close()


# Attach's write of the mode (docs/architecture/cells.md): the table lock first, so an
# insert that starts while it waits queues behind it.
_ATTACH_LOCK = "LOCK TABLE loom.tenancy_mode IN EXCLUSIVE MODE"
_ATTACH_WRITE = "UPDATE loom.tenancy_mode SET mode = 'enforced'"
_WAITING = "SELECT wait_event_type FROM pg_stat_activity WHERE pid = $1"


async def _waits_on_a_lock(watcher: asyncpg.Connection, pid: int) -> None:
    """Until backend `pid` waits on a lock (pg_stat_activity), at most 5 s."""
    for _ in range(100):
        if await watcher.fetchval(_WAITING, pid) == "Lock":
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"backend {pid} never waited on a lock")


@pytest.mark.asyncio
async def test_attachs_mode_write_and_a_recording_insert_never_cross() -> None:
    """A running insert holds the mode row, so attach's lock waits for it to commit; an
    insert that starts while attach waits queues behind it, then reads `enforced` and
    writes nothing. Restores the mode and rows it found."""
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    import asyncpg

    admin = await asyncpg.connect(dsn)
    if not await admin.fetchval("SELECT to_regclass('loom.tenancy') IS NOT NULL"):
        await admin.close()
        pytest.skip("this Postgres has not applied 008_cells.sql")
    a, b = (f"tenancy-race-{uuid.uuid4().hex[:8]}" for _ in range(2))
    racers: list[asyncpg.Connection] = []
    found, read = None, False
    try:
        found, read = await admin.fetchval("SELECT mode FROM loom.tenancy_mode"), True
        await admin.execute("DELETE FROM loom.tenancy_mode")
        await admin.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('recording')")
        # The isolation the cell's pool pins (server/core/pg.py).
        isolation = {"default_transaction_isolation": "read committed"}
        for _ in range(3):
            racers.append(await asyncpg.connect(dsn, server_settings=isolation))
        held, attach, late = racers
        # An insert is running: attach's lock waits for it.
        tx = held.transaction()
        await tx.start()
        assert await held.fetchval(tenancy._RECORD, a) == 1
        locked = attach.transaction()
        await locked.start()
        write = asyncio.ensure_future(attach.execute(_ATTACH_LOCK))
        await _waits_on_a_lock(admin, attach.get_server_pid())
        # An insert that starts now queues behind attach, not beside the running one.
        record = asyncio.ensure_future(late.fetchval(tenancy._RECORD, b))
        await _waits_on_a_lock(admin, late.get_server_pid())
        await tx.commit()
        await write
        await attach.execute(_ATTACH_WRITE)
        await locked.commit()
        assert await record is None  # it read `enforced`
        assert await admin.fetchval(tenancy._HOLDS, a) == 1
        assert await admin.fetchval(tenancy._HOLDS, b) is None
    finally:
        try:
            # End the racing backends first: one blocked in a lock wait would otherwise
            # finish after the cleanup and write a row back.
            pids = [c.get_server_pid() for c in racers]
            ended = "SELECT count(pg_terminate_backend(p, 5000)) FROM unnest($1::int[]) p"
            await admin.fetchval(ended, pids)
            for c in racers:
                c.terminate()
            await admin.execute("DELETE FROM loom.tenancy WHERE org = ANY($1::text[])", [a, b])
            if read:
                await admin.execute("DELETE FROM loom.tenancy_mode")
                if found is not None:
                    await admin.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ($1)", found)
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_the_first_row_cannot_be_changed_even_with_triggers_off_for_replication() -> None:
    """`loom.cell_first` is written once: an update, a delete or a truncate is refused,
    also under `session_replication_role = replica` (its triggers are `ENABLE ALWAYS`).
    Runs in one transaction it rolls back."""
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        if not await conn.fetchval("SELECT to_regclass('loom.cell_first') IS NOT NULL"):
            pytest.skip("this Postgres has not applied 008_cells.sql")
        if not await conn.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            pytest.skip("setting session_replication_role needs a superuser")
        tx = conn.transaction()
        await tx.start()
        try:
            if await conn.fetchval("SELECT count(*) FROM loom.cell_first") == 0:
                await conn.execute(
                    "INSERT INTO loom.cell_first (is_first, cell) VALUES (true, 'it-first')"
                )
            for role in ("origin", "replica"):
                await conn.execute(f"SET LOCAL session_replication_role = {role}")
                for sql in (
                    "UPDATE loom.cell_first SET cell = cell",
                    "DELETE FROM loom.cell_first",
                    "TRUNCATE loom.cell_first",
                ):
                    sp = conn.transaction()
                    await sp.start()
                    with pytest.raises(asyncpg.RaiseError, match="written once"):
                        await conn.execute(sql)
                    await sp.rollback()
        finally:
            await tx.rollback()
    finally:
        await conn.close()
