"""A process with LOOM_CELL unset reads its deployment's cell state from a real Postgres
and is gated by it (server/core/cell.py), and the troubleshooting page's state
transaction, run as the page prints it, takes a gated deployment back to `open`
(docs/operations/troubleshooting.md). Each test runs in a database of its own
(`_pg_database`)."""

from __future__ import annotations

import contextlib
import itertools
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from server.core import cell, pg
from server.core.schema import pg_migrate
from server.settings import settings
from tests.integration._pg_database import own_database, with_database

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


@contextlib.asynccontextmanager
async def _own_database() -> AsyncIterator[Any]:
    import asyncpg

    async with own_database() as dsn:
        conn = await asyncpg.connect(dsn)
        await pg.init_connection(conn)  # the pool's codecs: json decodes as the server reads it
        try:
            yield conn
        finally:
            await conn.close()


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


_ROWS = ("first", "mode", "own", "tenancy")
# Every mix of the four rows but none and the two full sets: the partial states.
PARTIAL = [
    set(c)
    for n in range(1, 5)
    for c in itertools.combinations(_ROWS, n)
    if set(c) not in ({"first", "mode", "own"}, {"first", "mode", "own", "tenancy"})
]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", PARTIAL, ids=["+".join(sorted(r)) for r in PARTIAL])
async def test_every_partial_state_gates_an_unset_process(
    monkeypatch: pytest.MonkeyPatch, rows: set[str]
) -> None:
    assert len(PARTIAL) == 13
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
        "SELECT tgenabled::text AS tgenabled FROM pg_trigger WHERE tgrelid = 'loom.cell_first'::regclass "
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
        holder = await asyncpg.connect(with_database(os.environ["LOOM_PG_DSN"], database))
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


def _holder_query() -> str:
    """The page's query naming the session that holds the attach lock."""
    text = PAGE.read_text()
    section = text[text.index("### A process with LOOM_CELL unset is gated") :]
    return next(b for b in re.findall(r"```sql\n(.*?)```", section, re.S) if "pg_locks" in b)


@pytest.mark.asyncio
async def test_the_pages_transaction_waits_for_no_running_attach_and_names_its_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attach, prepare or update holds the attach lock; the transaction takes it before
    the migration lock, so it refuses at once and changes nothing, and the page's query
    names the session holding it."""
    import asyncpg

    _, transaction = _page_sql()
    async with _own_database() as conn:
        await _write_state(conn, {"tenancy"})
        database = await conn.fetchval("SELECT current_database()")
        holder = await asyncpg.connect(with_database(os.environ["LOOM_PG_DSN"], database))
        try:
            await holder.execute("SELECT pg_advisory_lock($1)", cell.ATTACH_LOCK_KEY)
            with pytest.raises(asyncpg.exceptions.LockNotAvailableError):
                await conn.execute(_statements(transaction))
            await conn.execute("ROLLBACK")
            assert "partial" in await _gate(monkeypatch, conn)
            rows = await conn.fetch(_holder_query())
            assert [r["pid"] for r in rows] == [await holder.fetchval("SELECT pg_backend_pid()")]
            await holder.execute("SELECT pg_advisory_unlock($1)", cell.ATTACH_LOCK_KEY)
            assert await conn.fetch(_holder_query()) == []
            await conn.execute(_statements(transaction))  # the rerun
            assert await _gate(monkeypatch, conn) == ""
        finally:
            await holder.close()


@pytest.mark.asyncio
async def test_loom_cell_removes_check_reads_the_first_cells_postgres_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`loom cell remove`'s check of a first cell: one session from LOOM_PG_DSN, open and
    recording the row's deployment id, or it refuses."""
    import argparse

    import asyncpg
    import httpx

    from server.cli import cell as cell_cli
    from server.cli.cell_ch import Refused

    async with own_database() as dsn:
        monkeypatch.setattr(settings, "pg_dsn", dsn)
        monkeypatch.setattr(settings, "cell", "eu-1")
        monkeypatch.setattr(settings, "document_s3_bucket", "")
        monkeypatch.setattr(pg, "_expected", None)
        row = {
            "cell": "eu-1",
            "is_first": True,
            "host": "cell.example.test",
            "state": "attaching",
            "placed_orgs": 0,
            "deployment_id": "dep-1",
            "bucket": "",
            "bucket_prefix": "",
            "probe_url": "",
            "upstream_url": "",
            "current_fp": None,
            "next_fp": None,
            "mutation_generation": "00000000-0000-0000-0000-000000000001",
        }
        calls = []

        def router(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            if request.method == "GET" or request.url.path.endswith("/claim"):
                return httpx.Response(200, json=row)
            assert request.method == "DELETE"
            return httpx.Response(200, json={"cell": "eu-1", "removed": True})

        args = argparse.Namespace(cell="eu-1")
        url = httpx.URL("https://router.example.test")
        transport = httpx.MockTransport(router)
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "INSERT INTO loom.authority (authority, epoch, deployment_id) "
                "VALUES ('postgres', 1, 'dep-1')"
            )
            assert await cell_cli._remove_locked(args, url, transport) == {
                "cell": "eu-1",
                "removed": True,
            }
            assert calls == ["GET", "POST", "DELETE"]
            calls.clear()
            row["deployment_id"] = "dep-2"
            with pytest.raises(Refused, match="do not match the directory registration"):
                await cell_cli._remove_locked(args, url, transport)
            assert calls == ["GET"]
            assert await conn.fetchval("SELECT deployment_id FROM loom.authority") == "dep-1"
            calls.clear()
            row["deployment_id"] = "dep-1"
            await _write_state(conn, {"tenancy"})
            with pytest.raises(Refused, match="reads partial"):
                await cell_cli._remove_locked(args, url, transport)
            assert calls == ["GET"]
        finally:
            await conn.close()
