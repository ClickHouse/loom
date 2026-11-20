"""Postgres migrations: numbered SQL files in `schemas/pg_migrations/`, applied in
order at boot under a session advisory lock, each ordinary file in a transaction
with its ledger row (`loom.pg_migrations`, the file's SHA-256). Declared online indexes
run concurrently outside transactions under a separate session key. The router's directory
(`schemas/router_migrations/`, `server/router/directory.py`) migrates the same way,
on its own database, ledger schema and lock. For schema `loom`, `migrate` also marks
its session as one that ran the tenancy check and, after the files and before the lock
goes, runs `cell_triggers.ensure` (docs/architecture/cells.md).

The rules are the ClickHouse journal's (docs/development/migrations.md): additive
only, never edit a merged file. A ledger row whose checksum no longer matches its
file refuses the boot, as does an unknown file numbered at or below this build's
last migration. A build serves a newer schema, warning about unknown files numbered
above its last migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from asyncio import sleep
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from server.core import pg
from server.core.ch import set_query_tag
from server.core.paths import PG_MIGRATIONS_DIR
from server.core.schema import pg_online_index
from server.core.schema.journal import migration_number, sorted_migrations

log = logging.getLogger(__name__)

# A fixed key for pg_advisory_lock: every Loom process migrates under it.
_LOCK_KEY = 0x6C6F6F6D_6D696772  # "loommigr"


def _ledger(schema: str) -> str:
    # `schema` is one of this build's constants (`loom`, `loom_router`), never input.
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.pg_migrations (
    filename text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)"""


class MigrationDrift(RuntimeError):
    """An applied file changed, or the ledger holds a file the journal lost."""


def migration_files(mig_dir: Path = PG_MIGRATIONS_DIR) -> list[Path]:
    return sorted_migrations(mig_dir.glob("*.sql"))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan(applied: dict[str, str], files: list[Path]) -> list[Path]:
    """The files still to apply; raises MigrationDrift on a changed or lost file."""
    known = {f.name: f for f in files}
    last = max((migration_number(f.name) or 0 for f in files), default=0)
    for name, digest in applied.items():
        f = known.get(name)
        if f is None:
            if (migration_number(name) or 0) <= last:
                raise MigrationDrift(f"applied Postgres migration {name} is not in this build")
            continue
        if checksum(f) != digest:
            raise MigrationDrift(f"applied Postgres migration {name} was edited")
    newer = sorted(n for n in applied if n not in known)
    if newer:
        log.warning("Postgres schema has migrations this build does not carry: %s", newer)
    return [f for f in files if f.name not in applied]


async def pending(mig_dir: Path = PG_MIGRATIONS_DIR) -> list[str]:
    """The files `migrate` would apply, read-only: every file when the ledger is not
    there yet. For a process that applies none (`LOOM_BOOT_MIGRATE=0`)."""
    import asyncpg

    try:
        rows = await pg.fetch("SELECT filename, checksum FROM loom.pg_migrations")
    except asyncpg.exceptions.UndefinedTableError:
        rows = []
    applied = {r["filename"]: r["checksum"] for r in rows}
    return [f.name for f in plan(applied, migration_files(mig_dir))]


async def migrate(
    mig_dir: Path = PG_MIGRATIONS_DIR,
    *,
    connect: Callable[[], AbstractAsyncContextManager[Any]] | None = None,
    schema: str = "loom",
    lock_key: int = _LOCK_KEY,
) -> list[str]:
    """Apply every pending file; returns their names. Concurrent boots serialize on
    the advisory lock and the later ones find nothing pending. `connect`, `schema`
    and `lock_key` name another database's connection, ledger and lock (the router's
    directory); the defaults are the memory authority's.

    On a connection of its own, with no client command timeout and no statement
    timeout: DDL on a large table runs as long as it takes. A lock it waits for is
    bounded (`lock_timeout`), so an `ALTER` queued behind a long transaction never
    holds every read and write of the table behind it; the file is retried. For schema
    `loom` it marks the session and runs `cell_triggers.ensure` before the lock goes."""
    files, indexes = _validated(mig_dir, schema)
    cm = (connect or pg.dedicated)()
    conn = await cm.__aenter__()
    task = asyncio.create_task(
        _owned_result(lambda: _migrate(conn, files, indexes, schema, lock_key))
    )
    try:
        result = await asyncio.shield(task)
        if isinstance(result, (SystemExit, KeyboardInterrupt)):
            raise result
    except BaseException as error:  # Keep the migration failure through owned cleanup.
        end = time.monotonic() + pg._RELEASE_TIMEOUT_S
        await _failed_migration(cm, conn, task, error, end)
        raise
    await cm.__aexit__(None, None, None)
    return result


async def _owned_result[T](work: Callable[[], Awaitable[T]]) -> T | SystemExit | KeyboardInterrupt:
    try:
        return await work()
    except (SystemExit, KeyboardInterrupt) as error:
        # Transport native failures through child Tasks without stopping their supervisor.
        return error


async def _failed_migration(
    cm: AbstractAsyncContextManager[Any],
    conn: Any,
    task: asyncio.Task[Any],
    error: BaseException,
    end: float,
) -> None:
    try:
        await pg.close_owned(conn, end=end)
    except BaseException:  # noqa: BLE001 — preserve the migration failure/cancellation.
        pass
    if not task.done():
        task.cancel()
    task.add_done_callback(pg._retrieve_task)
    try:
        await asyncio.wait({task}, timeout=max(0, end - time.monotonic()))
    except BaseException:  # noqa: BLE001 — preserve the first failure on repeated cancellation.
        try:
            pg._terminate(conn)
        except BaseException:  # noqa: BLE001 — fallback abort cannot replace the migration failure.
            pass
    release = asyncio.create_task(
        _owned_result(lambda: cm.__aexit__(type(error), error, error.__traceback__))
    )
    try:
        done, _ = await asyncio.wait({release}, timeout=max(0, end - time.monotonic()))
        if not done:
            raise TimeoutError("migration context cleanup deadline expired")
        release.result()
    except BaseException:  # noqa: BLE001 — a context exit cannot replace the migration failure.
        try:
            pg._terminate(conn)
        except BaseException:  # noqa: BLE001 — fallback abort cannot replace the migration failure.
            pass
    finally:
        if not release.done():
            release.cancel()
        release.add_done_callback(pg._retrieve_task)


def _validated(
    mig_dir: Path, schema: str
) -> tuple[list[Path], dict[str, pg_online_index.OnlineIndex | None]]:
    files = migration_files(mig_dir)
    # Validate every online declaration before even creating the ledger.
    indexes = {f.name: pg_online_index.parse(f.read_text()) for f in files}
    if any(index is not None and index.schema != schema for index in indexes.values()):
        raise ValueError("online-index target differs from its ledger schema")
    return files, indexes


async def migrate_borrowed(
    conn: Any,
    mig_dir: Path = PG_MIGRATIONS_DIR,
    *,
    schema: str = "loom",
    lock_key: int = _LOCK_KEY,
) -> list[str]:
    """Apply on the owner's session; its supervisor owns all failure cleanup.

    The caller retains its operation deadline and must close the session before
    cancelling this work. Normal completion releases only the migration key.
    """
    files, indexes = _validated(mig_dir, schema)
    return await _migrate(conn, files, indexes, schema, lock_key)


async def _lock(conn: Any, key: int) -> None:
    while True:
        set_query_tag("migrate.postgres.lock")
        if await conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
            return
        # Each try ends its statement/snapshot before another attempt.
        await sleep(0.1)


async def _unlock(conn: Any, key: int) -> None:
    if not await conn.fetchval("SELECT pg_advisory_unlock($1)", key):
        raise RuntimeError("Postgres migration advisory lock was lost")


def _online_key(schema: str, lock_key: int) -> int:
    value = hashlib.sha256(f"loom.online-index:{schema}:{lock_key}".encode()).digest()
    key = int.from_bytes(value[:8], "big") & (2**63 - 1)
    if key == lock_key:
        raise ValueError("Postgres migration lock keys collide")
    return key


async def _applied(conn: Any, schema: str, files: list[Path]) -> dict[str, str]:
    rows = await conn.fetch(f"SELECT filename, checksum FROM {schema}.pg_migrations")
    applied = {r["filename"]: r["checksum"] for r in rows}
    plan(applied, files)
    return applied


async def _record_online(conn: Any, path: Path, schema: str, *, pending: bool) -> None:
    async with conn.transaction():
        if pending:
            await conn.execute(
                f"INSERT INTO {schema}.pg_migrations (filename, checksum) VALUES ($1, $2)",
                path.name,
                checksum(path),
            )
        # A visible ledger from a cancelled synchronous commit needs a fresh flush.
        await conn.execute(pg._DURABLE_MESSAGE)


async def _online(
    conn: Any,
    path: Path,
    index: pg_online_index.OnlineIndex,
    schema: str,
    lock_key: int,
    files: list[Path],
) -> bool:
    await _unlock(conn, lock_key)
    online_key = _online_key(schema, lock_key)
    await _lock(conn, online_key)
    applied = await _applied(conn, schema, files)
    await pg_online_index.build(conn, index, recorded=path.name in applied)
    # A shipped blocking migrator may wait here; the concurrent build is over.
    await _lock(conn, lock_key)
    applied = await _applied(conn, schema, files)
    pending = path.name not in applied
    await _record_online(conn, path, schema, pending=pending)
    await _unlock(conn, online_key)
    return pending


async def _migrate(
    conn: Any,
    files: list[Path],
    indexes: dict[str, pg_online_index.OnlineIndex | None],
    schema: str,
    lock_key: int,
) -> list[str]:
    done: list[str] = []
    await _lock(conn, lock_key)
    if schema == "loom":
        await conn.execute(f"SET {pg.TENANCY_MARKER} = 'on'")
    await conn.execute("SET lock_timeout = '5s'")
    await conn.execute("SET synchronous_commit = 'on'")
    await conn.execute(_ledger(schema))
    applied = await _applied(conn, schema, files)
    for path in files:
        set_query_tag("migrate.postgres.apply")
        index = indexes[path.name]
        if index is not None:
            changed = await _online(conn, path, index, schema, lock_key, files)
            applied = await _applied(conn, schema, files)
        else:
            changed = path.name not in applied
            if changed:
                await _apply(conn, path.name, path.read_text(), checksum(path), schema=schema)
        if changed:
            log.info("applied Postgres migration %s", path.name)
            done.append(path.name)
    for index in indexes.values():
        set_query_tag("migrate.postgres.verify")
        if index is not None and await pg_online_index.ready(conn, index) is not True:
            raise ValueError(f"recorded online-index {index.qualified} is absent or invalid")
    if schema == "loom":
        from server.core import cell_triggers

        await cell_triggers.ensure(conn)
    # A caller may lend its marked session and retain unrelated session keys.
    await _unlock(conn, lock_key)
    return done


async def _apply(conn: Any, name: str, sql: str, digest: str, *, schema: str = "loom") -> None:
    import asyncpg

    record = f"INSERT INTO {schema}.pg_migrations (filename, checksum) VALUES ($1, $2)"
    attempts = 5  # tries before the boot fails on a table lock held past `lock_timeout`
    for attempt in range(1, attempts + 1):
        try:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(record, name, digest)
            return
        except asyncpg.exceptions.LockNotAvailableError:
            if attempt == attempts:
                raise
            log.warning("Postgres migration %s waited too long for a lock; retrying", name)
            await sleep(attempt)
