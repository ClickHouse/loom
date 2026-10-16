"""Boot DDL never starts during a provider rebuild, and never runs under the provider lock."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from server import boot
from server.insights.app import provider_barrier
from server.insights.store import provider_configuration, provider_lock


@pytest.fixture
def setup(monkeypatch):
    client = AsyncMock()
    client.query_one.return_value = {"n": 1}
    migration = Mock(return_value=0)
    pending = Mock(return_value=["024_new.sql"])
    owned = []
    events = []

    @asynccontextmanager
    async def lock(*, wait_s=5):
        assert wait_s == boot.settings.boot_migrate_lease_wait_s
        events.append("lock")
        owned.append(True)
        try:
            yield
        finally:
            owned.pop()

    monkeypatch.setattr(provider_lock, "configuration_lock", lock)
    monkeypatch.setattr(
        provider_barrier,
        "enroll_before_migrations",
        AsyncMock(side_effect=lambda _ch: events.append("enrolled")),
    )
    client.events = events
    rows = AsyncMock(return_value=[])
    monkeypatch.setattr(provider_configuration, "configurations", rows)
    execute = Mock(return_value=0)
    monkeypatch.setattr(boot, "run_boot_migrations_if_enabled", execute)
    return client, migration, pending, rows, execute, owned


@pytest.mark.asyncio
async def test_migrations_run_under_the_migrate_lease_alone_after_enrolling(setup):
    """A replica killed mid-migration must not leave the provider lock held."""
    client, migrate, pending, _, execute, owned = setup

    def run(*args, **kwargs):
        assert not owned
        return 7

    execute.side_effect = run
    assert (
        await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending) == 7
    )
    assert client.events == ["enrolled", "lock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [[], ["024_new.sql"]])
async def test_pending_rebuild_never_runs_boot_ddl(setup, missing):
    client, migrate, pending, rows, execute, _ = setup
    rows.return_value = [{"state": "rebuilding"}]
    pending.return_value = missing
    if missing:
        with pytest.raises(RuntimeError, match="Finish the provider rebuild"):
            await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
    else:
        assert (
            await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
            == 0
        )
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_first_install_predates_provider_lock_table(setup):
    client, migrate, pending, rows, execute, owned = setup
    client.query_one.return_value = {"n": 0}
    await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
    execute.assert_called_once()
    rows.assert_not_awaited()
    assert not owned


@pytest.mark.asyncio
async def test_noop_restart_requires_no_keeper_and_never_invokes_migration_runner(setup):
    client, migrate, pending, rows, execute, _ = setup
    pending.return_value = []
    assert (
        await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending) == 0
    )
    client.query_one.assert_not_awaited()
    rows.assert_not_awaited()
    execute.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_migrations_require_no_new_schema_or_lock(setup):
    client, migrate, pending, rows, execute, _ = setup
    await boot.run_fenced_boot_migrations(client, migrate, enabled=False, pending=pending)
    client.query_one.assert_not_awaited()
    rows.assert_not_awaited()
    assert execute.call_args.kwargs["enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, {"n": "0"}, {"n": False}, {"n": 2}])
async def test_unknown_schema_status_never_bypasses_provider_fence(setup, unknown):
    client, migrate, pending, rows, execute, _ = setup
    client.query_one.return_value = unknown
    with pytest.raises(RuntimeError, match="Cannot establish"):
        await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
    execute.assert_not_called()
    rows.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [[], ["024_new.sql"]])
async def test_boot_forgets_a_pending_schema_a_sibling_applied(setup, missing):
    """A worker that cached a pending table while a sibling migrated must not
    keep refusing it, on the no-op path too."""
    from server.core import ch_schema_guard

    client, migrate, pending, _, _, _ = setup
    pending.return_value = missing
    ch_schema_guard._schema_guard["at"] = 1e12
    ch_schema_guard._schema_guard["tables"] = {"_provider_participants": "023_x.sql"}
    try:
        assert (
            await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
            == 0
        )
        assert ch_schema_guard._schema_guard["at"] is None
        assert ch_schema_guard._schema_guard["tables"] == {}
    finally:
        ch_schema_guard.schema_guard_reset()


@pytest.mark.asyncio
async def test_failed_boot_migrations_keep_the_cache(setup):
    from server.core import ch_schema_guard

    client, migrate, pending, _, execute, _ = setup
    execute.return_value = 3
    ch_schema_guard._schema_guard["at"] = 1e12
    try:
        assert (
            await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
            == 3
        )
        assert ch_schema_guard._schema_guard["at"] == 1e12
    finally:
        ch_schema_guard.schema_guard_reset()


@pytest.mark.asyncio
async def test_a_lock_still_missed_after_the_wait_fails_the_start(setup, monkeypatch):
    """The pending list is read once, before the lock: an empty read after a
    miss is no proof a sibling finished (the ledger read fails open)."""
    client, migrate, pending, _, execute, _ = setup

    @asynccontextmanager
    async def missed(**kwargs):
        pending.return_value = []
        raise provider_lock.ConfigurationLockUnavailable("provider_configuration_lock_unavailable")
        yield

    monkeypatch.setattr(provider_lock, "configuration_lock", missed)
    with pytest.raises(provider_lock.ConfigurationLockUnavailable):
        await boot.run_fenced_boot_migrations(client, migrate, enabled=True, pending=pending)
    pending.assert_called_once()
    execute.assert_not_called()
