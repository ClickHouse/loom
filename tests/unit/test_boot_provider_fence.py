"""Boot DDL and provider publication share a cross-process fence."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from server import boot
from server.insights.store import provider_configuration, provider_lock


@pytest.fixture
def setup(monkeypatch):
    client = AsyncMock()
    client.query_one.return_value = {"n": 1}
    migration = Mock(return_value=0)
    pending = Mock(return_value=["024_new.sql"])
    owned = []

    @asynccontextmanager
    async def lock():
        owned.append(True)
        try:
            yield
        finally:
            owned.pop()

    monkeypatch.setattr(provider_lock, "configuration_lock", lock)
    rows = AsyncMock(return_value=[])
    monkeypatch.setattr(provider_configuration, "configurations", rows)
    execute = Mock(return_value=0)
    monkeypatch.setattr(boot, "run_boot_migrations_if_enabled", execute)
    return client, migration, pending, rows, execute, owned


@pytest.mark.asyncio
async def test_installed_schema_migrations_hold_fence_for_whole_run(setup):
    client, migrate, pending, _, execute, owned = setup

    def run(*args, **kwargs):
        assert owned
        return 7

    execute.side_effect = run
    assert (
        await boot.run_fenced_boot_migrations(
            client, migrate, "lock", enabled=True, pending=pending
        )
        == 7
    )
    assert not owned


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [[], ["024_new.sql"]])
async def test_pending_rebuild_never_runs_boot_ddl(setup, missing):
    client, migrate, pending, rows, execute, _ = setup
    rows.return_value = [{"state": "rebuilding"}]
    pending.return_value = missing
    if missing:
        with pytest.raises(RuntimeError, match="Finish the provider rebuild"):
            await boot.run_fenced_boot_migrations(
                client, migrate, "lock", enabled=True, pending=pending
            )
    else:
        assert (
            await boot.run_fenced_boot_migrations(
                client, migrate, "lock", enabled=True, pending=pending
            )
            == 0
        )
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_first_install_predates_provider_lock_table(setup):
    client, migrate, pending, rows, execute, owned = setup
    client.query_one.return_value = {"n": 0}
    await boot.run_fenced_boot_migrations(client, migrate, "lock", enabled=True, pending=pending)
    execute.assert_called_once()
    rows.assert_not_awaited()
    assert not owned


@pytest.mark.asyncio
async def test_noop_restart_requires_no_keeper_and_never_invokes_migration_runner(setup):
    client, migrate, pending, rows, execute, _ = setup
    pending.return_value = []
    assert (
        await boot.run_fenced_boot_migrations(
            client, migrate, "lock", enabled=True, pending=pending
        )
        == 0
    )
    client.query_one.assert_not_awaited()
    rows.assert_not_awaited()
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_migrations_require_no_new_schema_or_lock(setup):
    client, migrate, pending, rows, execute, _ = setup
    await boot.run_fenced_boot_migrations(client, migrate, "lock", enabled=False, pending=pending)
    client.query_one.assert_not_awaited()
    rows.assert_not_awaited()
    assert execute.call_args.kwargs["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, {"n": "0"}, {"n": False}, {"n": 2}])
async def test_unknown_schema_status_never_bypasses_provider_fence(setup, unknown):
    client, migrate, pending, rows, execute, _ = setup
    client.query_one.return_value = unknown
    with pytest.raises(RuntimeError, match="Cannot establish"):
        await boot.run_fenced_boot_migrations(
            client, migrate, "lock", enabled=True, pending=pending
        )
    execute.assert_not_called()
    rows.assert_not_awaited()
