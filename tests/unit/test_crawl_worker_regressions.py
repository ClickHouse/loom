"""Native crawl turns retain absence proofs, pass bounds and mirror retry state."""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest

from server.clients import gdrive
from server.knowledge.app import crawl_authority, private_sync, source_sync_crawl
from server.knowledge.app import source_sync as sync
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, mirror_uri
from server.knowledge.store import documents
from server.memory.domain.provenance import pipeline_actor
from server.settings import settings
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_publication import _MIRROR
from tests.unit.test_crawl_authority import _BOUNDS
from tests.unit.test_crawl_composed_flow import _register, _World
from tests.unit.test_crawl_composed_flow import account as account
from tests.unit.test_crawl_composed_flow import key_view as key_view
from tests.unit.test_crawl_composed_flow import world as world
from tests.unit.test_crawl_worker_observation import _file

pytestmark = pytest.mark.asyncio


async def _work(
    world: _World, *, left: int = 10, asked: dict[str, Any] | None = None
) -> tuple[sync.TurnResult, sync.Pass]:
    assert world.source is not None
    now = time.monotonic()
    run = sync.Pass(sync.PassBudget(now, now + 20), left)
    with acting_as(None), pipeline_actor(SYNC_WORKER_ACTOR), private_sync.scope():
        result = await source_sync_crawl.turn(
            world.source, {"detail": json.dumps(asked or {})}, run, bounds=_BOUNDS
        )
    return result, run


async def _persist(world: _World, result: sync.TurnResult) -> dict[str, str]:
    assert world.source is not None and result.mirror == _MIRROR
    source = dataclasses.replace(world.source, mirror=_MIRROR)
    authority = await crawl_authority.resolve(
        source, deadline=time.monotonic() + 20, bounds=_BOUNDS
    )
    expansion = await authority.prepare()
    old = await sync.mirror_state(source, _MIRROR)
    with acting_as(None), pipeline_actor(SYNC_WORKER_ACTOR), private_sync.scope():
        async with authority.using(expansion.people), sync.publication_guard(source):
            await sync._write_mirrored_state(source, {}, result, {}, old)
    return await sync.mirror_state(source, _MIRROR)


def _copies(world: _World, monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    assert world.source is not None
    rows = [
        {
            "file_id": "missing-" + str(i),
            "source_uri": mirror_uri(
                "https://docs.google.com/document/d/missing-" + str(i), world.source.source_id
            ),
            "searchable": True,
            "source_revision": "old",
            "access_json": "{}",
        }
        for i in range(count)
    ]

    async def every_copy(org: str, mirror: str, tag: str):
        assert world.source is not None
        assert (org, mirror, tag) == ("team", _MIRROR, "source-id:" + world.source.source_id)
        return rows

    checked: list[str] = []

    async def foreign_document(org: str, namespace: str, uri: str, *, include_superseded: bool):
        assert (org, namespace, include_superseded) == ("team", _MIRROR, False)
        checked.append(uri)
        # Retirement still runs its real ownership validator and preserves a foreign latest row.
        return SimpleNamespace(actor="another-writer", tags=[])

    monkeypatch.setattr(sync.store, "every_copy", every_copy)
    monkeypatch.setattr(documents, "get_one", foreign_document)
    return checked


async def test_missing_page_file_requires_native_positive_absence(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(world)
    checked = _copies(world, monkeypatch, 1)
    original = world.handle
    lookups: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            if request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": [], "incompleteSearch": False})
            if request.url.path == "/drive/v3/files/missing-0":
                lookups.append(request.url.path)
                return httpx.Response(200, json=_file("missing-0"))
        return await original(request)

    world.handle = provider
    result, _ = await _work(world)
    assert lookups == ["/drive/v3/files/missing-0"]
    assert result.complete and not result.removed
    assert (
        not checked
    )  # Still reached by the configured docs filter; never considered for deletion.


@pytest.mark.parametrize("left", [0, 1])
async def test_absence_checks_and_retire_attempts_share_the_document_budget(
    world: _World, monkeypatch: pytest.MonkeyPatch, left: int
) -> None:
    await _register(world)
    checked = _copies(world, monkeypatch, 3)
    original = world.handle
    lookups: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            if request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": []})
            if request.url.path.startswith("/drive/v3/files/missing-"):
                lookups.append(request.url.path)
                return httpx.Response(404, json={"error": "notFound"})
        return await original(request)

    world.handle = provider
    result, run = await _work(world, left=left)
    assert run.left == 0 and result.cut and not result.complete
    assert len(lookups) == len(checked) == left
    assert not result.removed and not world.listings


async def test_empty_folder_query_needs_current_selected_folder_visibility(world: _World) -> None:
    await _register(world)
    assert world.source is not None
    world.source = dataclasses.replace(
        world.source, items=[{"type": "filter", "kinds": ["docs"], "in": "selected-folder"}]
    )
    await world.setup()  # Seal approval for the actual changed selection.
    original = world.handle
    checked: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com":
            if request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": []})
            if request.url.path == "/drive/v3/files/selected-folder":
                checked.append(request.url.path)
                return httpx.Response(404, json={"error": "notFound"})
        return await original(request)

    world.handle = provider
    result, _ = await _work(world)
    assert checked == ["/drive/v3/files/selected-folder"]
    assert not result.complete and not world.listings and not world.document_writes


async def test_consecutive_failed_exports_resume_the_mirror_wait_counter(world: _World) -> None:
    await _register(world)
    original = world.handle

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/file/export":
            return httpx.Response(500, json={"error": "temporary"})
        return await original(request)

    world.handle = provider
    first, _ = await _work(world)
    assert first.waits["file"][1] == 1 and not first.complete
    saved = await _persist(world, first)
    assert json.loads(saved["detail"])["file_waits"]["file"][1] == 1
    second, _ = await _work(world)
    assert second.waits["file"][1] == 2 and not second.complete
    assert not world.document_writes


@pytest.mark.parametrize("explicit_full", [False, True])
async def test_whole_enumeration_does_not_regrant_a_settled_revision_before_full_due(
    world: _World, explicit_full: bool
) -> None:
    await _register(world)
    assert world.source is not None
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    detail = {
        "full_listed_at": now,
        "file_waits": {"file": [_file()["modifiedTime"], 3, "too_large"]},
    }
    world.states.append(
        {
            "org": "team",
            "namespace": _MIRROR,
            "state_key": sync.store.state_key(world.source.source_id),
            "state_value": "",
            "detail": json.dumps(detail),
        }
    )
    original = world.handle
    exports: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/drive/v3/files/file/export":
            exports.append(request.url.path)
        return await original(request)

    world.handle = provider
    asked = (
        {"requested_at": "explicit-request", "full": True}
        if explicit_full
        else {"file_waits": {}, "full_listed_at": "forged-namespace-state"}
    )
    result, _ = await _work(world, asked=asked)
    assert result.documents_seen == 1  # Both paths still enumerate the whole union.
    assert result.full is explicit_full
    assert len(exports) == int(explicit_full)
    if explicit_full:
        assert result.waits_reset_for == "explicit-request" and result.changed == 1
    else:
        assert result.waits == detail["file_waits"] and result.changed == 0


async def test_one_presenters_absence_cannot_retire_another_presenters_reached_file(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(world)
    assert world.source is not None
    world.source = dataclasses.replace(
        world.source,
        crawl_options={
            **world.source.crawl_options,
            "subjects": [
                {"type": "person", "email": "one@example.com"},
                {"type": "person", "email": "two@example.com"},
            ],
        },
    )
    await world.setup()
    checked = _copies(world, monkeypatch, 1)
    original = world.handle
    lookups: list[str] = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "admin.googleapis.com" and request.url.path.rsplit("/", 1)[1] in {
            "two@example.com",
            "provider-two",
        }:
            return httpx.Response(
                200,
                json={
                    "id": "provider-two",
                    "primaryEmail": "two@example.com",
                    "aliases": [],
                    "customerId": "workspace",
                    "suspended": False,
                    "archived": False,
                },
            )
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            claims = jwt.decode(
                form["assertion"][0],
                world.account[1],
                algorithms=["RS256"],
                audience="https://oauth2.googleapis.com/token",
            )
            if claims["sub"] == "two@example.com":
                assert claims["scope"] == gdrive.ReadScope.DRIVE.value
                return httpx.Response(
                    200,
                    json={
                        "access_token": "second-person-access",
                        "token_type": "Bearer",
                        "expires_in": 60,
                    },
                )
        if request.url.host == "www.googleapis.com":
            if request.url.path == "/drive/v3/files":
                return httpx.Response(200, json={"files": []})
            if request.url.path == "/drive/v3/files/missing-0":
                bearer = request.headers["authorization"]
                lookups.append(bearer)
                if bearer == "Bearer private-person-access":
                    return httpx.Response(404, json={"error": "notFound"})
                assert bearer == "Bearer second-person-access"
                return httpx.Response(200, json=_file("missing-0"))
        return await original(request)

    world.handle = provider
    result, _ = await _work(world)
    assert lookups == ["Bearer private-person-access", "Bearer second-person-access"]
    assert result.complete and not result.removed and not checked


async def test_configured_file_bound_refuses_the_whole_native_turn_before_publication(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _register(world)
    monkeypatch.setattr(settings, "knowledge_crawl_max_files", 1)
    original = world.handle
    listed = []

    async def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.googleapis.com" and request.url.path == "/drive/v3/files":
            listed.append(request)
            return httpx.Response(
                200,
                json={
                    "files": [_file("first"), _file("second")],
                    "incompleteSearch": False,
                },
            )
        return await original(request)

    world.handle = provider
    result, _ = await _work(world)
    assert listed and result.status != "ok" and not result.complete
    assert not world.document_writes and not world.chunk_writes and not world.listings
    assert not world.embedded
