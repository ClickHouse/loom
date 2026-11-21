"""Native item listings build one bounded observation before any publication."""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import httpx
import pytest

from server.clients._vendor import CredentialRefused, TooLarge
from server.code.app.index.pass_budget import PassBudget
from server.knowledge.app import crawl_authority, source_sync_crawl
from server.knowledge.app import source_sync as sync
from tests.unit.test_crawl_authority import _BOUNDS, _World
from tests.unit.test_crawl_authority import account as account
from tests.unit.test_crawl_authority import crawl_world as crawl_world
from tests.unit.test_crawl_authority import key_view as key_view

pytestmark = [pytest.mark.asyncio, pytest.mark.claim_cap]


def _file(id: str = "file") -> dict[str, Any]:
    return {
        "id": id,
        "version": "1",
        "name": "Private meeting",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-01-01T00:00:00.000Z",
        "webViewLink": "https://docs.google.com/document/d/" + id,
        "permissions": [],
        "owners": [{"me": True, "emailAddress": "one@example.com"}],
    }


def _run() -> sync.Pass:
    now = time.monotonic()
    return sync.Pass(PassBudget(now, now + 10), 10)


async def _authority(world: _World) -> crawl_authority.Authority:
    assert world.source is not None
    return await crawl_authority.resolve(
        world.source, deadline=time.monotonic() + 10, bounds=_BOUNDS
    )


async def test_actual_native_single_item_observation_keeps_ownership_without_owner_arm(
    crawl_world: _World,
) -> None:
    original = crawl_world.handle

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/file":
            assert request.url.params["fields"].endswith("owners(me,emailAddress),trashed")
            crawl_world.native_requests.append(request)
            return httpx.Response(200, json=_file())
        return await original(request)

    crawl_world.handle = provider
    authority = await _authority(crawl_world)
    expansion, files = await source_sync_crawl.observe(authority, _run(), maximum_files=2)
    (file,) = files
    assert file.id == "file" and file.access.emails == ("one@example.com",)
    assert not file.access.owner and expansion.people[0].id == "provider-one"
    assert len(crawl_world.native_requests) == 1
    assert not crawl_world.writes  # No memory, ACL, group, listing or retirement writes.


async def test_current_source_context_is_required_and_cleared_on_failed_work(
    crawl_world: _World,
) -> None:
    authority = await _authority(crawl_world)
    expansion = await authority.prepare()
    assert crawl_world.source is not None
    with pytest.raises(CredentialRefused, match="checkout unavailable"):
        await sync.require_current(crawl_world.source)
    with pytest.raises(RuntimeError, match="extraction failed"):
        async with authority.using(expansion.people):
            await sync.require_current(crawl_world.source)
            raise RuntimeError("extraction failed")
    with pytest.raises(CredentialRefused, match="checkout unavailable"):
        await sync.require_current(crawl_world.source)


@pytest.mark.parametrize("incomplete", [True, False])
async def test_incomplete_search_or_global_file_cap_never_produces_union(
    crawl_world: _World,
    incomplete: bool,
) -> None:
    assert crawl_world.source is not None
    crawl_world.source = dataclasses.replace(
        crawl_world.source, items=[{"type": "filter", "kinds": ["docs"]}]
    )
    await crawl_world.setup()
    original = crawl_world.handle

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files":
            crawl_world.native_requests.append(request)
            second = "pageToken" in request.url.params
            return httpx.Response(
                200,
                json={
                    "files": [_file("second" if second else "first")],
                    "incompleteSearch": incomplete,
                    **({} if incomplete or second else {"nextPageToken": "next"}),
                },
            )
        return await original(request)

    crawl_world.handle = provider
    with pytest.raises(ValueError if incomplete else TooLarge):
        await source_sync_crawl.observe(await _authority(crawl_world), _run(), maximum_files=1)
    assert len(crawl_world.native_requests) == (1 if incomplete else 2)
    assert not crawl_world.writes


async def test_native_response_after_registrant_loss_is_not_a_complete_observation(
    crawl_world: _World,
) -> None:
    from server.operations.base import Role

    original = crawl_world.handle

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/file":
            crawl_world.native_requests.append(request)
            crawl_world.grant = Role.WRITER
            return httpx.Response(200, json=_file())
        return await original(request)

    crawl_world.handle = provider
    with pytest.raises(PermissionError):
        await source_sync_crawl.observe(await _authority(crawl_world), _run(), maximum_files=2)
    assert len(crawl_world.native_requests) == 1 and not crawl_world.writes
