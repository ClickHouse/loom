"""Connected native turns publish checked content and vouch only while still current."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from datetime import datetime
from typing import Any

import httpx
import pytest

from server.clients import notion
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import mirrors, private_sync
from server.knowledge.app import source_sync as sync
from server.knowledge.domain.access_list import AccessList, from_json
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, mirror_uri
from server.knowledge.store import documents
from server.knowledge.store import mirrors as mirror_store
from server.knowledge.store import sources as source_store
from server.memory.domain.provenance import pipeline_actor
from tests.unit.principals import acting_as
from tests.unit.test_connected_source_publication import _MIRROR
from tests.unit.test_connected_source_publication import _World as _PublicationWorld
from tests.unit.test_notion_source_sync import PAGE, _page, _para

_ADDRESS = "https://pages.example.com/connected-notes"
_TEXT = "The owner publishes this connected revision."
_ACCESS = "worker-private-access"
_REFRESH = "worker-private-refresh"


class _World(_PublicationWorld):
    """Fake only provider, registry, lease, embedding and persistent-store I/O."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
        super().__init__(monkeypatch, http)
        assert self.source is not None
        previous = self.rows.pop(self.key)
        account = replace(previous, kind="notion", scopes=())
        self.key = account.org, account.subject, account.kind
        self.rows[self.key] = account
        self.source = replace(
            self.source,
            kind="notion",
            config={"page_ids": [PAGE], "database_ids": []},
            items=[{"type": "page", "id": PAGE}],
        )
        self.selected_source = self.source
        self.request = self.request.model_copy(
            update={"source_uri": mirror_uri(_ADDRESS, self.source.source_id)}
        )
        self.listings: list[dict[str, Any]] = []
        self.debt_checks = 0
        self.lose_before_listing = False
        self.native_requests: list[httpx.Request] = []
        self.refresh_requests: list[httpx.Request] = []
        self.page = _page(PAGE, "2026-01-01T00:00:00.000Z", title="Connected notes")
        self.page["url"] = _ADDRESS
        monkeypatch.setattr(accounts.settings, "connect_notion_client_id", "page-client")
        monkeypatch.setattr(accounts.settings, "connect_notion_client_secret", "page-secret")
        monkeypatch.setattr(accounts.settings, "connect_notion_read_content_only", True)
        monkeypatch.setattr(notion, "get_client", lambda: http)
        monkeypatch.setattr(notion, "_next_request_at", {})
        monkeypatch.setattr(mirrors, "mirrors_of", self.mirror_rows)
        monkeypatch.setattr(mirror_store, "_default_client", lambda: self)
        monkeypatch.setattr(source_store, "read_states", self.read_states)
        monkeypatch.setattr(source_store, "source_revisions", self.source_revisions)
        monkeypatch.setattr(documents, "publications_owed", self.publications_owed)

    async def mirror_rows(self, org: str, namespaces: list[str], **kw: Any):
        assert (org, namespaces, kw) == ("team", ["shared-space"], {"live_only": True})
        return {_MIRROR: "shared-space"}

    async def read_states(self, org: str, namespace: str, names: list[str]):
        assert (org, namespace, names) == ("team", _MIRROR, ["source-id"])
        return {}

    async def source_revisions(
        self, org: str, namespace: str, tag: str, **_kw: Any
    ) -> list[dict[str, str]]:
        assert (org, namespace, tag) == ("team", _MIRROR, "source-id:source-id")
        return []

    async def no_prior_document(self, org: str, namespace: str, uri: str, **kw: Any):
        assert (org, namespace, uri) == ("team", _MIRROR, self.request.source_uri)
        assert set(kw) == {"include_superseded"}
        return None

    async def publications_owed(self, org: str, namespace: str, suffix: str) -> bool:
        assert (org, namespace, suffix) == ("team", _MIRROR, "#source-id")
        self.debt_checks += 1
        if self.lose_before_listing:
            assert len(self.document_writes) == 1
            self.end_generation()
        return False

    async def insert(self, table: str, rows: list[dict[str, Any]], **_kw: Any) -> None:
        assert table == mirror_store.LISTINGS
        self.assert_storage_leases()
        self.events.append("listing")
        self.listings.extend(dict(row) for row in rows)

    def end_generation(self) -> None:
        account = self.rows[self.key]
        self.fences[accounts._fence_key(account.connection_id)] = accounts._next_connection(
            account.connection_id, inactive=True
        )

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert not self.holders.get(asyncio.current_task(), 0)
        if request.url.path == "/v1/oauth/token":
            assert str(request.url) == "https://api.notion.com/v1/oauth/token"
            assert request.method == "POST"
            assert request.headers["authorization"] == (
                "Basic " + base64.b64encode(b"page-client:page-secret").decode()
            )
            assert json.loads(request.content) == {
                "grant_type": "refresh_token",
                "refresh_token": "source-private-refresh",
            }
            assert self.rows[self.key].refresh_id
            self.refresh_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": _ACCESS,
                    "refresh_token": _REFRESH,
                    "token_type": "bearer",
                },
            )
        assert request.url.host == "api.notion.com"
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer " + _ACCESS
        assert request.headers["Notion-Version"] == notion.API_VERSION
        self.native_requests.append(request)
        if request.url.path == f"/v1/pages/{PAGE}":
            return httpx.Response(200, json=self.page)
        assert request.url.path == f"/v1/blocks/{PAGE}/children"
        return httpx.Response(200, json={"results": [_para(_TEXT)], "has_more": False})


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch):
    async def provider(request: httpx.Request) -> httpx.Response:
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        state = _World(monkeypatch, http)
        yield state


async def _turn(state: _World) -> sync.TurnResult:
    assert state.selected_source is not None
    started = time.monotonic()
    run = sync.Pass(budget=sync.PassBudget(started, started + 60), left=4)
    with acting_as(None), pipeline_actor(SYNC_WORKER_ACTOR), private_sync.scope():
        return await sync._turn_of(state.selected_source)(state.selected_source, {}, run)


async def test_native_connected_notion_turn_publishes_real_rows_then_a_guarded_listing(
    world,
) -> None:
    result = await _turn(world)
    assert (result.status, result.complete, result.full, result.changed, result.documents_seen) == (
        "ok",
        True,
        True,
        1,
        1,
    )
    assert result.mirror == _MIRROR and result.unconverted == result.skipped == 0
    assert len(world.refresh_requests) == 1
    assert [request.url.path for request in world.native_requests] == [
        f"/v1/pages/{PAGE}",
        f"/v1/blocks/{PAGE}/children",
    ]
    assert len(world.document_writes) == len(world.chunk_writes) == len(world.listings) == 1
    row, chunk = world.document_writes[0], world.chunk_writes[0]
    assert row["content"].strip() == chunk["content"] == _TEXT
    assert row["title"] == "Connected notes"
    assert row["actor"] == SYNC_WORKER_ACTOR
    assert from_json(row["access_json"]) == AccessList(owner="person")
    assert row["version"] == chunk["doc_version"] and row["revision"] == 1
    for stored in (row, chunk):
        assert (stored["org"], stored["namespace"], stored["source_uri"]) == (
            "team",
            _MIRROR,
            world.request.source_uri,
        )
        assert (
            stored["agent"],
            stored["user_id"],
            stored["session_id"],
            stored["source_class"],
        ) == ("-", "-", "-", "imported")
        assert stored["tags"] == [
            "source-kind:design_doc",
            "source:documents",
            "source-id:source-id",
        ]
    metadata = json.loads(row["metadata"])
    assert metadata["source_kind"] == "notion" and metadata["source_name"] == "documents"
    assert metadata["ingested_from"] == PAGE and metadata["ingested_format"] == "blocks"
    assert metadata["source_revision"] == world.page["last_edited_time"]
    assert json.loads(chunk["doc_metadata"]) == metadata
    assert chunk["embedding"] == [1.0, 0.0, 0.0, 0.0]
    assert chunk["embedding_model"] == world.model_name and chunk["embedding_dim"] == 4
    listing = world.listings[0]
    assert {key: value for key, value in listing.items() if key != "started_at"} == {
        "org": "team",
        "namespace": _MIRROR,
        "source_id": "source-id",
        "name": "documents",
    }
    assert isinstance(listing["started_at"], datetime)
    assert world.debt_checks == 1 and world.events.index("head-end") < world.events.index("listing")
    assert world.blob_writes == world.chunk_retirements == []


async def test_native_connected_turn_cannot_vouch_after_owner_generation_ends(world) -> None:
    world.lose_before_listing = True
    with pytest.raises(accounts.OwnerUnavailable):
        await _turn(world)
    assert len(world.document_writes) == len(world.chunk_writes) == 1
    assert world.debt_checks == 1 and world.listings == []
    assert len(world.refresh_requests) == 1 and len(world.native_requests) == 2


async def test_native_connected_turn_rechecks_generation_after_embedding_before_final_rows(
    world,
) -> None:
    world.pause_embedding = True
    work = asyncio.create_task(_turn(world))
    try:
        await world.await_stage(world.embedding_entered, work)
        assert len(world.native_requests) == 2
        assert world.chunk_writes == world.document_writes == world.listings == []
        async with accounts.account_lease(*world.key):
            world.end_generation()
        world.embedding_release.set()
        with pytest.raises(accounts.OwnerUnavailable):
            await work
        assert world.chunk_writes == world.document_writes == world.listings == []
        assert len(world.chunk_retirements) == 1
        assert world.chunk_retirements[0][0][0] == world.request.source_uri
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
