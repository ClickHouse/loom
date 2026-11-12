"""A process with LOOM_CELL unset reads its deployment's cell state from a real Postgres
and is gated by it (server/core/cell.py), and the troubleshooting page's state
transaction, run as the page prints it, takes a gated deployment back to `open`
(docs/operations/troubleshooting.md). Each test runs in a database of its own,
migrated from empty, and drops it."""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from server.core import cell, pg
from server.core.schema import pg_migrate
from server.settings import settings

PAGE = Path(__file__).resolve().parents[2] / "docs/operations/troubleshooting.md"
_OWN = {"v": 1, "fields": {}}


def _page_sql() -> tuple[str, str]:
    """The section's two SQL blocks as printed: the directory check, the transaction."""
    text = PAGE.read_text()
    section = text[text.index("### A process with LOOM_CELL unset is gated") :]
    blocks = re.findall(r"```sql\n(.*?)```", section, re.S)
    return blocks[0], blocks[1]


def _statements(block: str) -> str:
    """The block without psql's own commands (`\\set`, `\\gset`, `\\connect`)."""
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("\\"))


def _with_db(dsn: str, name: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{name}"))


@contextlib.asynccontextmanager
async def _own_database() -> AsyncIterator[Any]:
    dsn = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    import asyncpg

    name = f"loom_cell_it_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(dsn)
    try:
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        except asyncpg.exceptions.InsufficientPrivilegeError:
            pytest.skip("this Postgres role cannot create a database")
        target = _with_db(dsn, name)

        @contextlib.asynccontextmanager
        async def _connect() -> AsyncIterator[Any]:
            conn = await asyncpg.connect(target)
            try:
                yield conn
            finally:
                await conn.close()

        await pg_migrate.migrate(connect=_connect)
        conn = await asyncpg.connect(target)
        await pg.init_connection(conn)  # the pool's codecs: json decodes as the server reads it
        try:
            yield conn
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _write_state(conn: Any, rows: set[str]) -> None:
    if "first" in rows:
        await conn.execute("INSERT INTO loom.cell_first (is_first, cell) VALUES (true, 'eu-1')")
    if "mode" in rows:
        await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')")
    if "own" in rows:
        await conn.execute(
            "INSERT INTO loom.cell_self (current_fp, next_fp) VALUES ($1::jsonb, NULL)", _OWN
        )
    if "tenancy" in rows:
        await conn.execute("INSERT INTO loom.tenancy (org) VALUES ('stray-org')")


async def _gate(monkeypatch: pytest.MonkeyPatch, conn: Any) -> str:
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(cell, "_view", cell._View())
    monkeypatch.setattr(pg, "fetchrow", conn.fetchrow)
    assert await cell.refresh() is True
    return cell.gate_reason()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        {"tenancy"},
        {"mode"},
        {"first"},
        {"own"},
        {"first", "mode"},
        {"first", "own"},
        {"mode", "own", "tenancy"},
    ],
)
async def test_every_partial_state_gates_an_unset_process(
    monkeypatch: pytest.MonkeyPatch, rows: set[str]
) -> None:
    async with _own_database() as conn:
        await _write_state(conn, rows)
        assert "partial cell state" in await _gate(monkeypatch, conn)


@pytest.mark.asyncio
async def test_a_never_prepared_deployment_and_a_recording_first_cell_serve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _own_database() as conn:
        assert await _gate(monkeypatch, conn) == ""
        await conn.execute("INSERT INTO loom.cell_first (is_first, cell) VALUES (true, 'eu-1')")
        await conn.execute("INSERT INTO loom.tenancy_mode (mode) VALUES ('recording')")
        await conn.execute(
            "INSERT INTO loom.cell_self (current_fp, next_fp) VALUES ($1::jsonb, NULL)", _OWN
        )
        await conn.execute("INSERT INTO loom.tenancy (org) VALUES ('recorded-org')")
        assert await _gate(monkeypatch, conn) == ""
        await conn.execute("UPDATE loom.tenancy_mode SET mode = 'enforced'")
        assert "enforced" in await _gate(monkeypatch, conn)


async def _triggers_always(conn: Any) -> bool:
    found = await conn.fetch(
        "SELECT tgname, tgenabled::text AS tgenabled FROM pg_trigger WHERE tgrelid = 'loom.cell_first'::regclass "
        "AND NOT tgisinternal"
    )
    return len(found) == 2 and all(r["tgenabled"] == "A" for r in found)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,origin",
    [
        ({"tenancy"}, False),  # a stray row on a deployment never meant to be a cell
        ({"first", "mode", "own", "tenancy"}, False),  # a full cell state
        ({"first", "mode", "own"}, True),  # both cell_first triggers left at origin
    ],
)
async def test_the_pages_transaction_takes_the_state_to_open(
    monkeypatch: pytest.MonkeyPatch, rows: set[str], origin: bool
) -> None:
    _, transaction = _page_sql()
    async with _own_database() as conn:
        await _write_state(conn, rows)
        if origin:
            await conn.execute("ALTER TABLE loom.cell_first ENABLE TRIGGER cell_first_immutable")
            await conn.execute("ALTER TABLE loom.cell_first ENABLE TRIGGER cell_first_no_truncate")
        assert await _gate(monkeypatch, conn) != ""
        await conn.execute(_statements(transaction))
        assert await _gate(monkeypatch, conn) == ""
        assert await _triggers_always(conn)


@pytest.mark.asyncio
async def test_the_pages_transaction_changes_nothing_while_a_migration_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncpg

    _, transaction = _page_sql()
    async with _own_database() as conn:
        await _write_state(conn, {"tenancy"})
        database = await conn.fetchval("SELECT current_database()")
        holder = await asyncpg.connect(_with_db(os.environ["LOOM_PG_DSN"], database))
        try:
            await holder.execute("SELECT pg_advisory_lock($1)", pg_migrate._LOCK_KEY)
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await conn.execute(_statements(transaction))
            await conn.execute("ROLLBACK")
            assert "partial" in await _gate(monkeypatch, conn)
            await holder.execute("SELECT pg_advisory_unlock($1)", pg_migrate._LOCK_KEY)
            await conn.execute(_statements(transaction))  # the rerun
            assert await _gate(monkeypatch, conn) == ""
        finally:
            await holder.close()


@pytest.mark.asyncio
async def test_the_pages_directory_check_reads_an_unattached_later_cell() -> None:
    """The check's two queries as printed, the directory's run where `\\connect` points
    (here the same server's router schema, migrated into the test's database)."""
    check, _ = _page_sql()
    # The directory query follows psql's `\gset` and `\connect` lines.
    queries = [line.strip() for line in check.splitlines() if line.strip()]
    first = queries[0].removesuffix("\\gset").strip()
    second = queries[2].removesuffix(";")
    async with _own_database() as conn:
        from server.core.paths import ROUTER_MIGRATIONS_DIR
        from server.router import directory

        @contextlib.asynccontextmanager
        async def _same() -> AsyncIterator[Any]:
            yield conn

        await pg_migrate.migrate(
            ROUTER_MIGRATIONS_DIR, connect=_same, schema="loom_router", lock_key=directory._LOCK_KEY
        )
        await conn.execute(
            "INSERT INTO loom.authority (authority, epoch, deployment_id) VALUES ('postgres', 1, 'dep-2')"
        )
        deployment = await conn.fetchval(first)
        assert deployment == "dep-2"
        await conn.execute(
            "INSERT INTO loom_router.cells (cell, is_first, host, state, credential_sha256, "
            "deployment_id) VALUES ('eu-2', false, 'eu-2.example.test', 'attaching', $1, $2)",
            "0" * 64,
            deployment,
        )
        row = await conn.fetchrow(second.replace(":'id'", "$1"), deployment)
        assert (row["is_first"], row["state"], row["placed"]) == (False, "attaching", 0)
