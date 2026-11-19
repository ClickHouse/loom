"""A cancelled online-index ledger commit survives a private standby promotion.

Only its dedicated CI pair enables this destructive drill. Configured failures
are errors; the adopted integration server's replication settings never change.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import pytest

from server.core.paths import ROUTER_MIGRATIONS_DIR
from server.core.schema import pg_migrate
from tests.integration._pg_database import create_database

PRIMARY = "loom-scale-index-drill-pg"
STANDBY = PRIMARY + "-standby"
APPLICATION = "online-index-durability"


async def _docker(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), 5)
        raise
    assert process.returncode == 0, stderr.decode()
    return stdout.decode().strip()


async def _sql(container: str, database: str, sql: str) -> str:
    return await _docker("exec", container, "psql", "-U", "loom", "-d", database, "-Atc", sql)


async def _waiting(observer, task) -> int:
    async with asyncio.timeout(8):
        for _ in range(800):
            assert not task.done(), "migration acknowledged before standby flush"
            pid = await observer.fetchval(
                "SELECT pid FROM pg_stat_activity WHERE application_name = $1 "
                "AND wait_event = 'SyncRep' AND query = 'COMMIT;'",
                APPLICATION,
            )
            if pid is not None:
                assert not task.done()
                return pid
            await asyncio.sleep(0.01)
    pytest.fail("migration did not reach its synchronous COMMIT")


async def _ownership(observer) -> None:
    identifier = str(await observer.fetchval("SELECT system_identifier FROM pg_control_system()"))
    for container in (PRIMARY, STANDBY):
        assert await _docker("inspect", "--format", "{{.Name}}", container) == "/" + container
        assert (
            await _sql(container, "loom", "SELECT system_identifier FROM pg_control_system()")
            == identifier
        )
    assert await _sql(STANDBY, "loom", "SELECT pg_is_in_recovery()") == "t"
    assert (
        await observer.fetchval(
            "SELECT count(*) FROM pg_stat_replication WHERE application_name = 'loom_standby' "
            "AND sync_state = 'sync' AND state = 'streaming'"
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.recovery
@pytest.mark.destructive
async def test_online_ledger_retry_survives_physical_standby_promotion(
    tmp_path, monkeypatch
) -> None:
    if not os.environ.get("LOOM_PG_INDEX_DRILL"):
        pytest.skip("LOOM_PG_INDEX_DRILL not set: needs its disposable physical standby")
    assert os.environ["LOOM_PG_INDEX_DRILL"] == "1"
    dsn = os.environ["LOOM_PG_INDEX_DRILL_DSN"]
    observer = await asyncpg.connect(dsn, timeout=5, command_timeout=5)
    tasks = []
    paused = False
    try:
        await _ownership(observer)
        _, target = await create_database(dsn)

        @contextlib.asynccontextmanager
        async def connect() -> AsyncIterator[asyncpg.Connection]:
            conn = await asyncpg.connect(
                target,
                timeout=5,
                server_settings={"application_name": APPLICATION, "synchronous_commit": "on"},
            )
            try:
                yield conn
            finally:
                conn.terminate()

        prefix = tmp_path / "old"
        prefix.mkdir()
        for source in sorted(ROUTER_MIGRATIONS_DIR.glob("*.sql"))[:3]:
            (prefix / source.name).write_bytes(source.read_bytes())

        async def migrate(files: Path = ROUTER_MIGRATIONS_DIR):
            return await pg_migrate.migrate(
                files, connect=connect, schema="loom_router", lock_key=8917423
            )

        await migrate(prefix)
        record = pg_migrate._record_online

        async def pause_before_ledger(*args, **kwargs):
            nonlocal paused
            await _docker("pause", STANDBY)
            paused = True
            await record(*args, **kwargs)

        monkeypatch.setattr(pg_migrate, "_record_online", pause_before_ledger)
        first = asyncio.create_task(migrate())
        tasks.append(first)
        pid = await _waiting(observer, first)
        # The index is acknowledged; the pending ledger transaction is not visible yet.
        async with connect() as conn:
            assert (
                await conn.fetchval(
                    "SELECT indisvalid AND indisready FROM pg_index "
                    "WHERE indexrelid = 'loom_router.placements_by_cell_org'::regclass"
                )
                is True
            )
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, 3)
        async with asyncio.timeout(5):
            for _ in range(500):
                if not await observer.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid = $1)", pid
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("cancelled migration retained backend locks")
        # Cancellation releases the commit wait before other sessions can see the ledger.
        async with connect() as conn:
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM loom_router.pg_migrations "
                    "WHERE filename = '004_placements_by_cell_org.sql'"
                )
                == 1
            )
        monkeypatch.setattr(pg_migrate, "_record_online", record)
        retry = asyncio.create_task(migrate())
        tasks.append(retry)
        await _waiting(observer, retry)
        await _docker("unpause", STANDBY)
        paused = False
        assert await asyncio.wait_for(retry, 8) == sorted(
            path.name
            for path in ROUTER_MIGRATIONS_DIR.glob("*.sql")
            if path.name > "004_placements_by_cell_org.sql"
        )
        observer.terminate()
        await _docker("kill", "--signal", "KILL", PRIMARY)
        await _docker(
            "exec",
            "--user",
            "postgres",
            STANDBY,
            "bash",
            "-c",
            'pg_ctl -D "$PGDATA" promote -w -t 10',
        )
        assert await _sql(STANDBY, "loom", "SELECT pg_is_in_recovery()") == "f"
        database = urlsplit(target).path.removeprefix("/")
        assert (
            await _sql(
                STANDBY,
                database,
                "SELECT count(*) FROM loom_router.pg_migrations "
                "WHERE filename = '004_placements_by_cell_org.sql' AND checksum = '"
                + pg_migrate.checksum(ROUTER_MIGRATIONS_DIR / "004_placements_by_cell_org.sql")
                + "'",
            )
            == "1"
        )
        assert (
            await _sql(
                STANDBY,
                database,
                "SELECT indisvalid AND indisready FROM pg_index "
                "WHERE indexrelid = 'loom_router.placements_by_cell_org'::regclass",
            )
            == "t"
        )
    finally:
        first = sys.exception()
        try:
            if paused:
                await _docker("unpause", STANDBY)
        except BaseException as error:  # noqa: BLE001 — attempt all cleanup; keep first failure.
            first = first or error
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            async with asyncio.timeout(5):
                await asyncio.gather(*tasks, return_exceptions=True)
        except BaseException as error:  # noqa: BLE001 — observer cleanup survives join failure.
            first = first or error
        try:
            observer.terminate()
        except BaseException as error:  # noqa: BLE001 — preserve the original drill failure.
            first = first or error
        if first is not None:
            raise first
