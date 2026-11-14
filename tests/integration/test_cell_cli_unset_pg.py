"""A CLI command that declares an org with LOOM_CELL unset reads its deployment's cell
state on a real Postgres, as a role holding only the grants the upgrade step names, and
is refused on a cell past `recording` (server/cli/_ch.py, docs/operations/upgrades.md).
Each test runs in a database of its own (`_pg_database`)."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest

from server.cli import _ch
from server.core import pg
from server.settings import settings
from tests.integration._pg_database import create_database, drop_database, own_database

# The upgrade step's grant set for a CLI with LOOM_CELL unset.
_GRANTS = (
    "GRANT USAGE ON SCHEMA loom TO {role}",
    "GRANT SELECT ON loom.cell_first, loom.tenancy_mode, loom.authority TO {role}",
    "GRANT SELECT (updated_at) ON loom.cell_self TO {role}",
    "GRANT SELECT (since) ON loom.tenancy TO {role}",
)


def _as(dsn: str, role: str) -> str:
    parts = urlsplit(dsn)
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(parts._replace(netloc=f"{role}:{role}@{host}{port}"))


@contextlib.asynccontextmanager
async def _cli_role(dsn: str, *, grants: tuple[str, ...] = _GRANTS) -> AsyncIterator[str]:
    """A login role holding `grants` on `dsn`'s database: its DSN. Roles are server-wide,
    so it is dropped with what it owns afterwards."""
    import asyncpg

    role = f"loom_cli_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{role}'")
        for grant in grants:
            await admin.execute(grant.format(role=role))
        yield _as(dsn, role)
    finally:
        await admin.execute(f"DROP OWNED BY {role}")
        await admin.execute(f"DROP ROLE {role}")
        await admin.close()


def _unset(monkeypatch: pytest.MonkeyPatch, dsn: str) -> None:
    monkeypatch.setattr(settings, "cell", "")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", dsn)
    pg.expect_deployment(None)


async def _admin(dsn: str, *statements: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_the_upgrade_steps_grants_read_the_state_and_refuse_an_enforced_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with own_database() as dsn, _cli_role(dsn) as cli:
        _unset(monkeypatch, cli)
        await _ch.check_unset_cell()  # never prepared: open
        await _admin(
            dsn,
            "INSERT INTO loom.cell_first (is_first, cell) VALUES (true, 'eu-1')",
            "INSERT INTO loom.tenancy_mode (mode) VALUES ('recording')",
            'INSERT INTO loom.cell_self (current_fp) VALUES (\'{"v": 1, "fields": {}}\')',
        )
        await _ch.check_unset_cell()  # a first cell still recording
        await _admin(dsn, "UPDATE loom.tenancy_mode SET mode = 'enforced'")
        with pytest.raises(_ch.OrgNotHeld, match=r"enforced state \(cell 'eu-1'\)"):
            await _ch.check_unset_cell()


@pytest.mark.asyncio
async def test_a_role_missing_a_grant_refuses_rather_than_reading_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with own_database() as dsn, _cli_role(dsn, grants=_GRANTS[:2]) as cli:
        _unset(monkeypatch, cli)
        with pytest.raises(_ch.OrgNotHeld, match="could not be read"):
            await _ch.check_unset_cell()


@pytest.mark.asyncio
async def test_a_database_without_the_state_tables_reads_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema that predates the cell tables cannot be a cell: an N+1 CI image against
    it is not refused."""
    import os

    admin = os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    name, dsn = await create_database(admin)
    try:
        await _admin(dsn, "CREATE SCHEMA loom")
        async with _cli_role(dsn, grants=_GRANTS[:1]) as cli:
            _unset(monkeypatch, cli)
            await _ch.check_unset_cell()
    finally:
        await drop_database(admin, name)
