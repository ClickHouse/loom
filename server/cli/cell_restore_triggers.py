"""Strict guard repair after restore has reconstructed the cell's metadata."""

from __future__ import annotations

import asyncio
from typing import Any

from server.cli import cell_attach_pg as apg
from server.cli import cell_restore_pg as local
from server.core import cell, cell_triggers
from server.core.schema import pg_migrate


async def repair(conn: Any, descriptor: dict[str, Any]) -> None:
    if not await apg.lock_held(conn):
        raise ValueError("restore lost its operator lock before trigger repair")
    while True:
        if await conn.fetchval("SELECT pg_try_advisory_lock($1)", pg_migrate._LOCK_KEY):
            break
        await asyncio.sleep(0.1)
    rows = await conn.fetch("SELECT filename, checksum FROM loom.pg_migrations")
    if pg_migrate.plan({r["filename"]: r["checksum"] for r in rows}, pg_migrate.migration_files()):
        raise ValueError("Postgres schema is still pending before trigger repair")
    await _tables(conn, descriptor)
    defects = await cell_triggers.missing(conn) + await cell_triggers.first_missing(conn)
    if defects:
        raise ValueError("; ".join(d.text for d in defects))
    await local.transaction(conn, lambda: _nothing())
    await conn.execute("SELECT pg_advisory_unlock($1)", pg_migrate._LOCK_KEY)


async def _nothing() -> None:
    return None


async def _tables(conn: Any, descriptor: dict[str, Any]) -> None:
    await cell_triggers.ensure(conn)
    state = await cell.read_state(conn)
    mode = await conn.fetchval("SELECT mode FROM loom.tenancy_mode")
    for table in await cell_triggers.planned_tables(conn):
        if not await apg.lock_held(conn):
            raise ValueError("restore lost its operator lock during trigger repair")
        if state == "partial":
            outcome = await cell_triggers.ensure_restore_missing_own_table(
                conn,
                table,
                cell_name=descriptor["cell"],
                is_first=descriptor["is_first"],
                mode=mode,
            )
        else:
            outcome = await cell_triggers.ensure_table(
                conn, table, recording_first=True, attempts=1
            )
        if outcome.answer != "applied":
            raise ValueError(f"loom.{table}: trigger repair {outcome.answer}; {outcome.holders}")
