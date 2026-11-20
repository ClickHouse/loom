"""Missed owner lifecycle hooks retire only the checked source generation."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from server.core.errors import ValidationError
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import source_sync as sync
from server.knowledge.app import source_sync_connected
from server.operations.base import Role
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_authority import _KEY
from tests.unit.test_connected_source_operations import _World


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    async def provider(request: httpx.Request) -> httpx.Response:
        raise AssertionError("local owner repair must perform no provider request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        yield _World(monkeypatch, http)


@pytest.mark.parametrize("loss", ("writer", "scim", "stopped-generation"))
async def test_confirmed_owner_loss_retires_only_the_checked_source(world, loss: str) -> None:
    source = world.source
    assert source is not None
    account = world.rows[_KEY]
    if loss == "writer":
        world.roles["person"] = Role.VIEWER
    elif loss == "scim":
        world.scim_ended = True
    else:
        world.fences[accounts._fence_key(account.connection_id)] = accounts._next_connection(
            account.connection_id, inactive=True
        )
    with acting_as(None), pytest.raises(sync.SourceGone):
        await source_sync_connected.ensure_current(source)
    assert world.rows[_KEY] == account
    assert world.source is not None and not world.source.active
    assert world.source.source_id == source.source_id
    assert len(world.source_writes) == len(world.state_writes) == 1
    assert world.retirement_rows
    assert world.refresh_requests == world.native_requests == []


async def test_missing_fence_refuses_without_retiring_unverified_owner(world) -> None:
    source = world.source
    assert source is not None
    account = world.rows[_KEY]
    world.fences.clear()
    with acting_as(None), pytest.raises(ValidationError):
        await source_sync_connected.ensure_current(source)
    assert world.rows[_KEY] == account and world.source == source
    assert world.source_writes == world.state_writes == world.retirement_rows == []


async def test_a_reused_source_name_is_not_retired_as_the_original_generation(world) -> None:
    original = world.source
    assert original is not None
    world.roles["person"] = Role.VIEWER
    replacement = replace(original, source_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
    world.source = replacement
    with acting_as(None), pytest.raises(sync.SourceGone):
        await source_sync_connected.ensure_current(original)
    assert world.source == replacement and replacement.active
    assert world.source_writes == world.state_writes == world.retirement_rows == []
