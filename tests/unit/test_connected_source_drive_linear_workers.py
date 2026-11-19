"""Connected native turns retain checked provider ACLs through guarded publication."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Literal
from urllib.parse import parse_qs

import httpx
import pytest

from server.clients import gdrive, linear
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.domain.access_list import AccessList, from_json
from server.knowledge.domain.sources import SYNC_WORKER_ACTOR, mirror_uri
from server.knowledge.store import documents
from server.knowledge.store import sources as source_store
from tests.unit.test_connected_source_authority import _REFRESH as _INITIAL_REFRESH
from tests.unit.test_connected_source_authority import _SCOPES as _DRIVE_SCOPES
from tests.unit.test_connected_source_publication import _MIRROR
from tests.unit.test_connected_source_workers import _turn
from tests.unit.test_connected_source_workers import _World as _NotionWorld

_PROJECT = "11111111-1111-4111-8111-111111111111"
_TEAM = "22222222-2222-4222-8222-222222222222"
_DOCUMENT = "33333333-3333-4333-8333-333333333333"
_DRIVE_FILE = "connected-native-document"
_TEXT = "The connected worker keeps the provider's current permission list."
_ACCESS = "drive-linear-worker-access"
_REPLACEMENT = "drive-linear-worker-refresh"
_REVISION = "2026-01-01T00:00:00.000Z"
_KINDS = ("gdrive", "linear")


class _World(_NotionWorld):
    """Reuse real authority/publication code, replacing only external I/O."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        http: httpx.AsyncClient,
        kind: Literal["gdrive", "linear"],
    ) -> None:
        super().__init__(monkeypatch, http)
        assert self.source is not None
        self.kind = kind
        self.address = (
            "https://docs.google.com/document/d/connected-native-document/edit"
            if kind == "gdrive"
            else "https://linear.app/example/document/connected-native-document"
        )
        previous = self.rows.pop(self.key)
        account = replace(
            previous,
            kind=kind,
            scopes=_DRIVE_SCOPES if kind == "gdrive" else ("read",),
        )
        self.key = account.org, account.subject, kind
        self.rows[self.key] = account
        self.source = replace(
            self.source,
            kind=kind,
            config={"include": ["docs"]} if kind == "gdrive" else {"project_ids": [_PROJECT]},
            items=[{"type": "file", "id": _DRIVE_FILE}]
            if kind == "gdrive"
            else [{"type": "project", "id": _PROJECT}],
        )
        self.selected_source = self.source
        self.request = self.request.model_copy(
            update={"source_uri": mirror_uri(self.address, self.source.source_id)}
        )
        self.graphql: list[str] = []
        self.permission_pages: list[str] = []
        monkeypatch.setattr(accounts.settings, f"connect_{kind}_client_id", "worker-client")
        monkeypatch.setattr(accounts.settings, f"connect_{kind}_client_secret", "worker-secret")
        monkeypatch.setattr(gdrive, "get_client", lambda: http)
        monkeypatch.setattr(linear, "get_client", lambda: http)
        monkeypatch.setattr(source_store, "every_copy", self.every_copy)
        monkeypatch.setattr(documents, "committed_lists", self.committed_lists)

    async def every_copy(self, org: str, namespace: str, tag: str):
        assert (org, namespace, tag) == ("team", _MIRROR, "source-id:source-id")
        return []

    async def committed_lists(self, org: str, namespace: str, uris: list[str]):
        assert (org, namespace, uris) == ("team", _MIRROR, [])
        return {}

    async def handle(self, request: httpx.Request) -> httpx.Response:
        assert not self.holders.get(asyncio.current_task(), 0)
        token_url = (
            "https://oauth2.googleapis.com/token"
            if self.kind == "gdrive"
            else "https://api.linear.app/oauth/token"
        )
        if str(request.url) == token_url:
            assert request.method == "POST"
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["refresh_token"],
                "refresh_token": [_INITIAL_REFRESH],
                "client_id": ["worker-client"],
                "client_secret": ["worker-secret"],
            }
            assert self.rows[self.key].refresh_id
            self.refresh_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": _ACCESS,
                    "refresh_token": _REPLACEMENT,
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": " ".join(_DRIVE_SCOPES) if self.kind == "gdrive" else "read",
                },
            )
        assert request.headers["authorization"] == "Bearer " + _ACCESS
        self.native_requests.append(request)
        return self.drive(request) if self.kind == "gdrive" else self.tracker(request)

    def drive(self, request: httpx.Request) -> httpx.Response:
        assert request.url.scheme == "https" and request.url.host == "www.googleapis.com"
        assert request.method == "GET"
        path = f"/drive/v3/files/{_DRIVE_FILE}"
        if request.url.path == path:
            assert request.url.params["fields"] == f"{gdrive.LISTED_WITH_PERMISSIONS},trashed"
            assert request.url.params["supportsAllDrives"] == "true"
            # No inline share list: the actual client must fetch every permission page.
            return httpx.Response(
                200,
                json={
                    "id": _DRIVE_FILE,
                    "name": "Connected native document",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": _REVISION,
                    "webViewLink": self.address,
                    "trashed": False,
                },
            )
        if request.url.path == path + "/permissions":
            assert request.url.params["supportsAllDrives"] == "true"
            assert request.url.params["pageSize"] == "100"
            page = request.url.params.get("pageToken", "")
            self.permission_pages.append(page)
            if not page:
                return httpx.Response(
                    200,
                    json={
                        "permissions": [
                            {
                                "type": "user",
                                "role": "reader",
                                "emailAddress": "Reader@EXAMPLE.COM",
                            },
                            {
                                "type": "user",
                                "role": "reader",
                                "emailAddress": "deleted@example.com",
                                "deleted": True,
                            },
                        ],
                        "nextPageToken": "second-permission-page",
                    },
                )
            assert page == "second-permission-page"
            return httpx.Response(
                200,
                json={
                    "permissions": [
                        {"type": "anyone", "role": "reader", "allowFileDiscovery": False},
                        {
                            "type": "domain",
                            "role": "reader",
                            "domain": "link-only.example",
                            "allowFileDiscovery": False,
                        },
                        {
                            "type": "domain",
                            "role": "reader",
                            "domain": "WORKSPACE.EXAMPLE",
                            "allowFileDiscovery": True,
                        },
                    ]
                },
            )
        assert request.url.path == path + "/export"
        assert request.url.params["mimeType"] == "text/markdown"
        return httpx.Response(200, text=_TEXT, headers={"Content-Type": "text/markdown"})

    def tracker(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.linear.app/graphql"
        assert request.method == "POST"
        body = json.loads(request.content)
        operation = body["query"].split("query ", 1)[1].split("(", 1)[0]
        self.graphql.append(operation)
        variables = body["variables"]
        page = {"hasNextPage": False, "endCursor": None}
        if operation == "ProjectTeams":
            assert variables == {"id": _PROJECT, "first": linear.PAGE_SIZE, "after": None}
            data = {"project": {"teams": {"nodes": [{"id": _TEAM}], "pageInfo": page}}}
        elif operation == "TeamMembers":
            assert "includeDisabled: true" in body["query"]
            assert variables == {"id": _TEAM, "first": linear.PAGE_SIZE, "after": None}
            nodes = [
                {
                    "id": "44444444-4444-4444-8444-444444444444",
                    "email": "Reader@EXAMPLE.COM",
                    "active": True,
                },
                {
                    "id": "55555555-5555-4555-8555-555555555555",
                    "email": "disabled@example.com",
                    "active": False,
                },
                {"id": "66666666-6666-4666-8666-666666666666", "email": "unknown@example.com"},
            ]
            data = {"team": {"members": {"nodes": nodes, "pageInfo": page}}}
        elif operation == "ProjectDocuments":
            assert variables == {"id": _PROJECT, "first": linear.PAGE_SIZE}
            node = {
                "id": _DOCUMENT,
                "title": "Connected native document",
                "updatedAt": _REVISION,
                "url": self.address,
            }
            data = {"project": {"id": _PROJECT, "documents": {"nodes": [node], "pageInfo": page}}}
        else:
            assert operation == "Document" and variables == {"id": _DOCUMENT}
            data = {
                "document": {
                    "id": _DOCUMENT,
                    "content": _TEXT,
                    "updatedAt": _REVISION,
                    "url": self.address,
                }
            }
        return httpx.Response(200, json={"data": data})


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    async def provider(message: httpx.Request) -> httpx.Response:
        return await state.handle(message)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        state = _World(monkeypatch, http, request.param)
        yield state


def _expected_acl(world: _World) -> AccessList:
    return AccessList(
        owner="person",
        emails=("reader@example.com",),
        domains=("workspace.example",) if world.kind == "gdrive" else (),
    )


def _assert_native_reads(world: _World) -> None:
    assert len(world.refresh_requests) == 1
    if world.kind == "gdrive":
        assert world.permission_pages == ["", "second-permission-page"]
        assert [request.url.path for request in world.native_requests] == [
            f"/drive/v3/files/{_DRIVE_FILE}",
            f"/drive/v3/files/{_DRIVE_FILE}/permissions",
            f"/drive/v3/files/{_DRIVE_FILE}/permissions",
            f"/drive/v3/files/{_DRIVE_FILE}/export",
        ]
    else:
        assert world.graphql == ["ProjectTeams", "TeamMembers", "ProjectDocuments", "Document"]


@pytest.mark.parametrize("world", _KINDS, indirect=True)
async def test_connected_native_provider_content_keeps_checked_acl_and_listing(world) -> None:
    result = await _turn(world)
    assert (result.status, result.complete, result.full, result.changed, result.documents_seen) == (
        "ok",
        True,
        True,
        1,
        1,
    )
    assert result.mirror == _MIRROR and result.unconverted == result.skipped == 0
    _assert_native_reads(world)
    assert len(world.document_writes) == len(world.chunk_writes) == len(world.listings) == 1
    row, chunk = world.document_writes[0], world.chunk_writes[0]
    assert row["content"].strip() == chunk["content"] == _TEXT
    assert row["actor"] == SYNC_WORKER_ACTOR
    assert from_json(row["access_json"]) == _expected_acl(world)
    assert row["version"] == chunk["doc_version"] and row["revision"] == 1
    for stored in (row, chunk):
        assert (stored["org"], stored["namespace"], stored["source_uri"]) == (
            "team",
            _MIRROR,
            world.request.source_uri,
        )
        assert stored["tags"] == [
            "source-kind:design_doc",
            "source:documents",
            "source-id:source-id",
        ]
        assert (
            stored["agent"],
            stored["user_id"],
            stored["session_id"],
            stored["source_class"],
        ) == ("-", "-", "-", "imported")
    metadata = json.loads(row["metadata"])
    assert metadata["source_kind"] == world.kind
    assert metadata["ingested_from"] == (_DRIVE_FILE if world.kind == "gdrive" else _DOCUMENT)
    assert metadata["source_revision"] == _REVISION and metadata["ingested_format"] == "markdown"
    assert json.loads(chunk["doc_metadata"]) == metadata
    assert chunk["embedding"] == [1.0, 0.0, 0.0, 0.0]
    assert chunk["embedding_model"] == world.model_name and chunk["embedding_dim"] == 4
    assert world.debt_checks == (2 if world.kind == "gdrive" else 1)
    assert world.events.index("head-end") < world.events.index("listing")
    assert world.listings[0]["namespace"] == _MIRROR
    assert world.listings[0]["source_id"] == "source-id"
    assert world.blob_writes == world.chunk_retirements == []


@pytest.mark.parametrize("world", _KINDS, indirect=True)
async def test_connected_native_provider_generation_loss_during_embedding_cannot_publish(
    world,
) -> None:
    world.pause_embedding = True
    work = asyncio.create_task(_turn(world))
    try:
        await world.await_stage(world.embedding_entered, work)
        _assert_native_reads(world)
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
