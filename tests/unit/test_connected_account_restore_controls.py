"""Restored histories lose exact authority without changing unrelated registrations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from server.cli import restore as cli_restore
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import connected_restore
from tests.unit.test_connected_account_restore import (
    _RestoreWorld,
    _unrelated_restore_participants,
)
from tests.unit.test_connected_source_authority import _KEY


@pytest.fixture
async def world(monkeypatch):
    async def provider(request):
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        state = _RestoreWorld(monkeypatch, http)
        yield state


async def test_source_only_historical_owner_is_fenced_retired_and_physically_purged(world):
    account = world.rows.pop(_KEY)
    world.account_history.clear()
    await connected_restore.restore_all(apply=True, ch=world)
    assert not world.rows and not world.account_history and not world.source_history
    assert world.retirement_rows
    assert (
        world.fences[accounts._fence_key(account.connection_id)].partition("/")[2].startswith("0")
    )
    assert world.revokes == world.refresh_requests == world.native_requests == []


async def test_all_historical_random_prefixes_end_including_retained_newer_live_keeper(world):
    original = world.rows[_KEY]
    another = replace(original, connection_id="d" * 32 + "/1" + "e" * 31)
    world.account_history.append(another)
    first_key, second_key = (
        accounts._fence_key(original.connection_id),
        accounts._fence_key(another.connection_id),
    )
    world.fences[first_key] = "a" * 32 + "/1" + "c" * 31
    world.fences[second_key] = another.connection_id
    await connected_restore.restore_all(apply=True, ch=world)
    assert all(
        world.fences[key].partition("/")[2].startswith("0") for key in (first_key, second_key)
    )
    assert not world.rows and not world.account_history and not world.source_history
    assert world.retirement_rows and len(world.revokes) == 1


async def test_old_connected_history_retires_without_overwriting_foreign_shared_replacement(world):
    original = world.source
    assert original is not None
    replacement = replace(
        original,
        source_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        credential_mode="shared",
        owner_subject="foreign",
        connection_id="",
        access="namespace",
        credential_enc="foreign-credential",
    )
    world.source = replacement
    await connected_restore.restore_all(apply=True, ch=world)
    assert world.source == replacement
    assert world.source_writes == []
    assert not world.rows and not world.account_history and not world.source_history
    assert world.retirement_rows


@pytest.mark.parametrize("failure", ("malformed", "unavailable"))
async def test_unverified_keeper_cleanup_refuses_before_publication(monkeypatch, world, failure):
    account = world.rows[_KEY]
    if failure == "malformed":
        world.fences[accounts._fence_key(account.connection_id)] = "unverified-generation"
    else:

        async def unreadable(*args, **kwargs):
            raise RuntimeError("keeper unavailable")

        monkeypatch.setattr(accounts.claims, "get", unreadable)
    published = []
    _unrelated_restore_participants(monkeypatch, lambda: published.append(True))
    result = await cli_restore.reconcile("clickhouse", datetime.now(UTC), apply=True, ch=world)
    assert result != 0 and not published
    assert world.rows[_KEY] == account and len(world.source_history) == 1
    assert world.writes == world.source_writes == world.retirement_rows == world.revokes == []


async def test_dry_run_discovers_history_without_changing_any_authority(world):
    rows, history, source_history, fences = (
        dict(world.rows),
        list(world.account_history),
        list(world.source_history),
        dict(world.fences),
    )
    await connected_restore.restore_all(apply=False, ch=world)
    assert world.rows == rows and world.account_history == history
    assert world.source_history == source_history and world.fences == fences
    assert world.writes == world.source_writes == world.retirement_rows == world.revokes == []


async def test_legacy_live_override_does_not_override_connection_offline_check(monkeypatch, world):
    query_one = world.query_one

    async def live_worker(query, params=None, **kwargs):
        if "FROM loom.worker_status" in query:
            return {"n": 1}
        return await query_one(query, params, **kwargs)

    monkeypatch.setattr(world, "query_one", live_worker)
    published = []
    _unrelated_restore_participants(monkeypatch, lambda: published.append(True))
    account = world.rows[_KEY]
    result = await cli_restore.reconcile(
        "clickhouse", datetime.now(UTC), apply=True, live=True, ch=world
    )
    assert result != 0 and not published
    assert world.rows[_KEY] == account and len(world.source_history) == 1
    assert world.writes == world.source_writes == world.retirement_rows == world.revokes == []


async def test_failed_final_residue_verification_holds_publication_and_remote_revocation(
    monkeypatch, world
):
    query_one = world.query_one

    async def unverifiable(query, params=None, **kwargs):
        if "SELECT count() AS n FROM loom.connected_accounts" in query and params is None:
            assert not world.rows and not world.source_history
            return {"n": 1}
        return await query_one(query, params, **kwargs)

    monkeypatch.setattr(world, "query_one", unverifiable)
    published = []
    _unrelated_restore_participants(monkeypatch, lambda: published.append(True))
    result = await cli_restore.reconcile("clickhouse", datetime.now(UTC), apply=True, ch=world)
    assert result != 0 and not published
    assert not world.rows and not world.source_history
    assert world.retirement_rows and not world.revokes
