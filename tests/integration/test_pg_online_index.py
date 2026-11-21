"""Online index retries and migrator coexistence on scratch Postgres databases."""

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
from server.core.paths import ROUTER_MIGRATIONS_DIR
from server.core.schema import pg_migrate, pg_online_index
from server.router import directory
from server.settings import settings
from tests.integration._pg_database import create_database, drop_database

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def database(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[Any, Any]]:
    import asyncpg

    admin = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    name, dsn = await create_database(admin)
    monkeypatch.setattr(settings, "router_pg_dsn", dsn)
    monkeypatch.setattr(directory, "_pool", None)

    @contextlib.asynccontextmanager
    async def connect() -> AsyncIterator[Any]:
        conn = await asyncpg.connect(dsn, server_settings={"application_name": "online-index-test"})
        try:
            yield conn
        finally:
            await conn.close()

    async with contextlib.AsyncExitStack() as cleanup:
        cleanup.push_async_callback(drop_database, admin, name)
        cleanup.push_async_callback(directory.close)
        try:
            async with connect() as conn:
                yield conn, connect
        except BaseException:  # Owned cleanup cannot replace the first failure.
            with contextlib.suppress(BaseException):
                await cleanup.aclose()
            raise


def _prefix(tmp_path: Path) -> Path:
    prefix = tmp_path / "old"
    prefix.mkdir()
    for source in sorted(ROUTER_MIGRATIONS_DIR.glob("*.sql"))[:3]:
        (prefix / source.name).write_bytes(source.read_bytes())
    return prefix


def _journal() -> list[str]:
    return sorted(path.name for path in ROUTER_MIGRATIONS_DIR.glob("*.sql"))


def _after_prefix() -> list[str]:
    return _journal()[3:]


async def _migrate(connect: Any, files: Path = ROUTER_MIGRATIONS_DIR) -> list[str]:
    return await pg_migrate.migrate(files, connect=connect, schema="loom_router", lock_key=8917423)


async def test_online_migration_installs_and_retries_from_both_schemas(database, tmp_path) -> None:
    conn, connect = database
    await _migrate(connect, _prefix(tmp_path))
    assert await _migrate(connect) == _after_prefix()
    assert await _migrate(connect) == []
    index = pg_online_index.parse(
        (ROUTER_MIGRATIONS_DIR / "004_placements_by_cell_org.sql").read_text()
    )
    assert index is not None and await pg_online_index.ready(conn, index) is True
    assert await conn.fetchval("SELECT to_regclass('loom_router.placements_by_cell') IS NOT NULL")


async def test_install_from_empty(database) -> None:
    _, connect = database
    assert await _migrate(connect) == _journal()


async def _until(conn: Any, sql: str, *args: Any) -> None:
    async with asyncio.timeout(8):
        for _ in range(800):
            if await conn.fetchval(sql, *args):
                return
            await asyncio.sleep(0.01)
    pytest.fail("Postgres did not reach the expected migration state")


def _old_runner():
    """The shipped blocking runner, kept as source for the rolling-upgrade proof."""
    import importlib.util

    path = Path(__file__).parent / "fixtures" / "pg_migrate_n_minus_one.py"
    spec = importlib.util.spec_from_file_location("old_pg_migrate", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_old_snapshot_old_blocking_migrator_and_two_new_migrators(database, tmp_path) -> None:
    conn, connect = database
    old_files = _prefix(tmp_path)
    await _migrate(connect, old_files)
    async with connect() as snapshot:
        tx = snapshot.transaction(isolation="repeatable_read")
        await tx.start()
        await snapshot.fetchval("SELECT count(*) FROM loom_router.placements")
        first = asyncio.create_task(_migrate(connect))
        second = None
        try:
            await _until(
                conn,
                "SELECT EXISTS(SELECT 1 FROM pg_stat_progress_create_index "
                "WHERE datid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND phase = 'waiting for old snapshots')",
            )
            second = asyncio.create_task(_migrate(connect))
            # The ordinary key is free while CREATE INDEX waits for this old snapshot.
            # Running this original blocking implementation would deadlock if it weren't.
            old = _old_runner()
            async with asyncio.timeout(2):
                assert (
                    await old.migrate(
                        old_files, connect=connect, schema="loom_router", lock_key=8917423
                    )
                    == []
                )
            assert not first.done() and not second.done()
            # Even while the index waits, the old serving path can write and read.
            await conn.execute(
                "INSERT INTO loom_router.cells "
                "(cell, host, state, credential_sha256, deployment_id) "
                "VALUES ('a', 'a.example.test', 'serving', repeat('a', 64), 'dep-a')"
            )
            from tests.integration.fixtures import router_placement_n_minus_one as serving

            assert await serving.place("org-a", "a", lambda _: "") == ("a", "dep-a")
            assert await serving.lookup("org-a") == ("a", "a.example.test")
            assert await conn.fetchval("SELECT placed FROM loom_router.cells WHERE cell = 'a'") == 1
            assert (
                await conn.fetchval("SELECT count(*) FROM loom_router.audit WHERE action = 'place'")
                == 1
            )
            await tx.rollback()
            async with asyncio.timeout(5):
                results = await asyncio.gather(first, second)
            assert sorted(results) == [[], _after_prefix()]
        finally:
            if snapshot.is_in_transaction():
                await tx.rollback()
            for task in (first, second):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(t for t in (first, second) if t is not None), return_exceptions=True
            )


async def test_cancelled_build_leaves_a_matching_invalid_index_and_retry_repairs_it(
    database, tmp_path
) -> None:
    conn, connect = database
    await _migrate(connect, _prefix(tmp_path))
    async with connect() as snapshot:
        tx = snapshot.transaction(isolation="repeatable_read")
        await tx.start()
        await snapshot.fetchval("SELECT count(*) FROM loom_router.placements")
        migrating = asyncio.create_task(_migrate(connect))
        try:
            await _until(
                conn,
                "SELECT EXISTS(SELECT 1 FROM pg_stat_progress_create_index "
                "WHERE datid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                "AND phase = 'waiting for old snapshots')",
            )
            migrating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(migrating, 2)
            assert await conn.fetchval(
                "SELECT NOT indisvalid FROM pg_index "
                "WHERE indexrelid = 'loom_router.placements_by_cell_org'::regclass"
            )
            assert not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM loom_router.pg_migrations "
                "WHERE filename = '004_placements_by_cell_org.sql')"
            )
        finally:
            await tx.rollback()
            if not migrating.done():
                migrating.cancel()
                await asyncio.gather(migrating, return_exceptions=True)
    assert await _migrate(connect) == _after_prefix()
    assert await _migrate(connect) == []


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE TABLE loom_router.placements_by_cell_org (id int)",
        "CREATE INDEX placements_by_cell_org ON loom_router.placements (org, cell)",
        'CREATE INDEX placements_by_cell_org ON loom_router.placements (cell, org COLLATE "POSIX")',
        "CREATE UNIQUE INDEX placements_by_cell_org ON loom_router.placements (cell, org)",
        "CREATE INDEX placements_by_cell_org ON loom_router.placements (cell, org) WHERE org > 'm'",
        "CREATE INDEX placements_by_cell_org ON loom_router.placements (cell, org) INCLUDE(placed_at)",
        "CREATE INDEX placements_by_cell_org ON loom_router.cells (cell, host)",
        "CREATE INDEX placements_by_cell_org ON loom_router.placements (cell DESC, org)",
        "CREATE INDEX placements_by_cell_org ON loom_router.placements (cell text_pattern_ops, org)",
        "CREATE INDEX placements_by_cell_org ON loom_router.placements (cell, org) WITH (fillfactor=70)",
    ],
)
async def test_foreign_same_named_object_is_never_dropped(database, tmp_path, definition) -> None:
    conn, connect = database
    await _migrate(connect, _prefix(tmp_path))
    await conn.execute(definition)
    oid = await conn.fetchval("SELECT 'loom_router.placements_by_cell_org'::regclass::oid")
    with pytest.raises(ValueError, match="different definition"):
        await _migrate(connect)
    assert await conn.fetchval("SELECT 'loom_router.placements_by_cell_org'::regclass::oid") == oid


async def test_recorded_invalid_index_and_checksum_drift_refuse(database, tmp_path) -> None:
    conn, connect = database
    await _migrate(connect)
    await conn.execute("DROP INDEX loom_router.placements_by_cell_org")
    with pytest.raises(ValueError, match="recorded online-index"):
        await _migrate(connect)
    await conn.execute(
        "UPDATE loom_router.pg_migrations SET checksum = repeat('0', 64) "
        "WHERE filename = '004_placements_by_cell_org.sql'"
    )
    with pytest.raises(pg_migrate.MigrationDrift):
        await _migrate(connect)


async def test_cancel_after_catalog_completion_reuses_the_valid_index(
    database, tmp_path, monkeypatch
) -> None:
    conn, connect = database
    await _migrate(connect, _prefix(tmp_path))
    original = pg_online_index.build

    async def completed_then_cancel(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(pg_online_index, "build", completed_then_cancel)
    with pytest.raises(asyncio.CancelledError):
        await _migrate(connect)
    oid = await conn.fetchval("SELECT 'loom_router.placements_by_cell_org'::regclass::oid")
    monkeypatch.setattr(pg_online_index, "build", original)
    assert await _migrate(connect) == _after_prefix()
    assert await conn.fetchval("SELECT 'loom_router.placements_by_cell_org'::regclass::oid") == oid


async def test_keyset_plan_uses_the_ordered_cell_index(database) -> None:
    conn, connect = database
    await _migrate(connect)
    await conn.execute(
        "INSERT INTO loom_router.cells (cell, host, state, credential_sha256, deployment_id) "
        "SELECT 'cell-' || n, 'cell-' || n || '.example.test', 'serving', "
        "md5(n::text) || md5(n::text), 'dep-' || n FROM generate_series(1, 32) n"
    )
    await conn.execute(
        "INSERT INTO loom_router.placements(org, cell) "
        "SELECT 'org-' || lpad(n::text, 5, '0'), 'cell-' || (n % 32 + 1) "
        "FROM generate_series(1, 4096) n"
    )
    await conn.execute("VACUUM ANALYZE loom_router.placements")
    plan = await conn.fetch(
        "EXPLAIN SELECT org FROM loom_router.placements "
        "WHERE cell = 'cell-2' AND org > 'org-01000' ORDER BY org LIMIT 10"
    )
    text = "\n".join(r[0] for r in plan)
    assert "placements_by_cell_org" in text and "Sort" not in text


async def test_ordinary_files_still_rollback_with_the_ledger(database, tmp_path) -> None:
    conn, connect = database
    (tmp_path / "001_one.sql").write_text("CREATE TABLE loom_router.one (id int);")
    (tmp_path / "002_two.sql").write_text("CREATE TABLE loom_router.two (id int); SELECT 1 / 0;")
    import asyncpg

    with pytest.raises(asyncpg.DivisionByZeroError):
        await _migrate(connect, tmp_path)
    assert await conn.fetchval("SELECT to_regclass('loom_router.one') IS NOT NULL")
    assert not await conn.fetchval("SELECT to_regclass('loom_router.two') IS NOT NULL")
    assert await conn.fetchval("SELECT count(*) FROM loom_router.pg_migrations") == 1
    (tmp_path / "002_two.sql").write_text("CREATE TABLE loom_router.two (id int);")
    assert await _migrate(connect, tmp_path) == ["002_two.sql"]


async def test_invalid_online_declaration_fails_before_the_ledger_exists(
    database, tmp_path
) -> None:
    conn, connect = database
    (tmp_path / "001_bad.sql").write_text("-- loom:online-index bad\nSELECT 1;")
    with pytest.raises(ValueError, match="online-index"):
        await _migrate(connect, tmp_path)
    assert not await conn.fetchval("SELECT to_regclass('loom_router.pg_migrations') IS NOT NULL")


async def test_cancelled_concurrent_drop_is_retryable(database, tmp_path) -> None:
    conn, connect = database
    await _migrate(connect, _prefix(tmp_path))
    index = pg_online_index.parse(
        (ROUTER_MIGRATIONS_DIR / "004_placements_by_cell_org.sql").read_text()
    )
    assert index is not None
    await conn.execute(index.sql)
    async with connect() as reader:
        tx = reader.transaction()
        await tx.start()
        await reader.execute("SET LOCAL enable_seqscan = off")
        plan = await reader.fetch(
            "EXPLAIN SELECT org FROM loom_router.placements WHERE cell = 'a' ORDER BY org"
        )
        assert "placements_by_cell_org" in "\n".join(r[0] for r in plan)
        await reader.fetch("SELECT org FROM loom_router.placements WHERE cell = 'a' ORDER BY org")

        async def direct_drop():
            async with connect() as dropping:
                try:
                    await dropping.execute(
                        "DROP INDEX CONCURRENTLY loom_router.placements_by_cell_org"
                    )
                finally:
                    await pg.close_owned(dropping, end=time.monotonic() + 1)

        dropping = asyncio.create_task(direct_drop())
        try:
            query = (
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                "WHERE datname = current_database() AND query LIKE 'DROP INDEX CONCURRENTLY%' "
                "AND wait_event IS NOT NULL)"
            )
            await _until(conn, query)
            # Cancel after catalog invalidation, not an earlier lock/I/O wait.
            await _until(
                conn,
                "SELECT NOT indisvalid FROM pg_index "
                "WHERE indexrelid='loom_router.placements_by_cell_org'::regclass",
            )
            dropping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(dropping, 2)
            assert await pg_online_index.ready(conn, index) is False
            dropping = asyncio.create_task(_migrate(connect))
            await _until(conn, query)
            dropping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(dropping, 2)
        finally:
            await tx.rollback()
            if not dropping.done():
                dropping.cancel()
                await asyncio.gather(dropping, return_exceptions=True)
    assert await _migrate(connect) == _after_prefix()


async def test_try_lock_wait_has_no_retained_transaction_or_snapshot(database) -> None:
    conn, connect = database
    await conn.execute("SELECT pg_advisory_lock(8917423::bigint)")
    migrating = asyncio.create_task(_migrate(connect))
    try:
        await _until(
            conn,
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND query = 'SELECT pg_try_advisory_lock($1)' "
            "AND state = 'idle' AND backend_xmin IS NULL AND xact_start IS NULL)",
        )
        assert not migrating.done()
        await conn.execute("SELECT pg_advisory_unlock(8917423::bigint)")
        assert await asyncio.wait_for(migrating, 5) == _journal()
    finally:
        await conn.execute("SELECT pg_advisory_unlock(8917423::bigint)")
        if not migrating.done():
            migrating.cancel()
        await asyncio.gather(migrating, return_exceptions=True)


async def test_online_validation_is_repeated_after_later_files(database, tmp_path) -> None:
    _, connect = database
    for source in ROUTER_MIGRATIONS_DIR.glob("*.sql"):
        (tmp_path / source.name).write_bytes(source.read_bytes())
    (
        tmp_path / f"{max(int(name.split('_')[0]) for name in _journal()) + 1:03d}_damage.sql"
    ).write_text("DROP INDEX loom_router.placements_by_cell_org;")
    with pytest.raises(ValueError, match="recorded online-index"):
        await _migrate(connect, tmp_path)
