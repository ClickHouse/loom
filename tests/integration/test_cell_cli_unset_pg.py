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
    # Restored at teardown, so an armed deployment check never leaks into a later test.
    monkeypatch.setattr(pg, "_expected", None)


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
        with pytest.raises(_ch.OrgNotHeld, match=r"enforced mode \(cell 'eu-1'\)"):
            await _ch.check_unset_cell()


@pytest.mark.asyncio
async def test_a_role_missing_a_grant_refuses_rather_than_reading_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with own_database() as dsn, _cli_role(dsn, grants=_GRANTS[:2]) as cli:
        _unset(monkeypatch, cli)
        with pytest.raises(_ch.OrgNotHeld, match="could not be read .*permission denied"):
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


@pytest.mark.asyncio
async def test_the_deployment_check_runs_on_the_state_session_with_the_steps_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state session is the command's first Postgres connection, so it runs the
    deployment check: the step's `SELECT` on `loom.authority` is what it needs, and
    another deployment's database refuses."""
    async with own_database() as dsn, _cli_role(dsn) as cli:
        await _admin(
            dsn,
            "INSERT INTO loom.authority (authority, epoch, deployment_id) "
            "VALUES ('postgres', 1, 'dep-a')",
        )
        _unset(monkeypatch, cli)
        monkeypatch.setattr(pg, "_expected", ("dep-a", 1))
        await _ch.check_unset_cell()
        monkeypatch.setattr(pg, "_expected", ("dep-b", 1))
        with pytest.raises(_ch.OrgNotHeld, match="could not be read"):
            await _ch.check_unset_cell()


@pytest.mark.asyncio
async def test_a_full_connection_limit_is_retried_then_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Postgres's refusal for too many connections reaches the retry as SQLSTATE 53300:
    a role at its limit is tried `_BUSY_TRIES` times, then refused."""
    import asyncpg

    async with own_database() as dsn, _cli_role(dsn) as cli:
        role = urlsplit(cli).username
        await _admin(dsn, f"ALTER ROLE {role} CONNECTION LIMIT 0")
        _unset(monkeypatch, cli)
        monkeypatch.setattr(_ch, "_BUSY_PAUSE_S", 0.0)
        opens: list[int] = []
        connect = asyncpg.connect

        async def _counted(*args: object, **kwargs: object) -> object:
            opens.append(1)
            return await connect(*args, **kwargs)

        monkeypatch.setattr(asyncpg, "connect", _counted)
        with pytest.raises(_ch.OrgNotHeld, match="could not be read"):
            await _ch.check_unset_cell()
        assert len(opens) == _ch._BUSY_TRIES


@pytest.mark.asyncio
async def test_a_write_by_a_role_holding_only_the_column_grants_is_a_permission_error() -> None:
    """On an enforced cell the org trigger reads `loom.tenancy.org` as the writer: a role
    holding only `SELECT (since)` there is refused with SQLSTATE 42501, not LT001, as
    the upgrade step says."""
    import asyncpg

    from server.core import cell_triggers

    async with own_database() as dsn:
        await _admin(
            dsn,
            "INSERT INTO loom.cell_first (is_first, cell) VALUES (true, 'eu-1')",
            "INSERT INTO loom.tenancy_mode (mode) VALUES ('enforced')",
            'INSERT INTO loom.cell_self (current_fp) VALUES (\'{"v": 1, "fields": {}}\')',
        )
        admin = await asyncpg.connect(dsn, server_settings={"loom.tenancy_checked": "on"})
        try:
            await cell_triggers.ensure(admin)
        finally:
            await admin.close()
        grants = (*_GRANTS, "GRANT INSERT ON loom.scope_generations TO {role}")
        async with _cli_role(dsn, grants=grants) as cli:
            plain = await asyncpg.connect(cli)
            try:
                with pytest.raises(asyncpg.PostgresError) as refused:
                    await plain.execute(
                        "INSERT INTO loom.scope_generations (org, scope, generation) "
                        "VALUES ('stray-org', 's', 1)"
                    )
            finally:
                await plain.close()
        assert refused.value.sqlstate == "42501"
