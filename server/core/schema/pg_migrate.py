"""Postgres migrations: numbered SQL files in `schemas/pg_migrations/`, applied in
order at boot under a session advisory lock, each file in one transaction with
its ledger row (`loom.pg_migrations`, the file's SHA-256).

The rules are the ClickHouse journal's (docs/development/migrations.md): additive
only, never edit a merged file. A ledger row whose checksum no longer matches its
file refuses the boot, as does a ledger row for a file this build does not carry
when that file's number is above the build's last one (a newer build's schema is
served, as ClickHouse's is; an unknown lower number means the journal was edited).
"""

from __future__ import annotations

import hashlib
import logging
from asyncio import sleep
from pathlib import Path
from typing import Any

from server.core import pg
from server.core.paths import PG_MIGRATIONS_DIR
from server.core.schema.journal import migration_number, sorted_migrations

log = logging.getLogger(__name__)

# A fixed key for pg_advisory_lock: every Loom process migrates under it.
_LOCK_KEY = 0x6C6F6F6D_6D696772  # "loommigr"

_LEDGER = """
CREATE SCHEMA IF NOT EXISTS loom;
CREATE TABLE IF NOT EXISTS loom.pg_migrations (
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


async def migrate(mig_dir: Path = PG_MIGRATIONS_DIR) -> list[str]:
    """Apply every pending file; returns their names. Concurrent boots serialize on
    the advisory lock and the later ones find nothing pending.

    On a connection of its own, with no client command timeout and no statement
    timeout: DDL on a large table runs as long as it takes. A lock it waits for is
    bounded (`lock_timeout`), so an `ALTER` queued behind a long transaction never
    holds every read and write of the table behind it; the file is retried."""
    files = migration_files(mig_dir)
    done: list[str] = []
    # Closing the connection ends the session, and with it the advisory lock.
    async with pg.dedicated() as conn:
        # Another boot migrating holds the lock for as long as its files take.
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        await conn.execute("SET lock_timeout = '5s'")
        await conn.execute(_LEDGER)
        rows = await conn.fetch("SELECT filename, checksum FROM loom.pg_migrations")
        for name, sql, digest in _read(plan({r["filename"]: r["checksum"] for r in rows}, files)):
            await _apply(conn, name, sql, digest)
            log.info("applied Postgres migration %s", name)
            done.append(name)
    return done


def _read(files: list[Path]) -> list[tuple[str, str, str]]:
    return [(f.name, f.read_text(), checksum(f)) for f in files]


async def _apply(conn: Any, name: str, sql: str, digest: str) -> None:
    import asyncpg

    record = "INSERT INTO loom.pg_migrations (filename, checksum) VALUES ($1, $2)"
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
