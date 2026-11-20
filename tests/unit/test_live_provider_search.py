"""Live hits retain native corpus membership and bounded checked transport."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest

from server.clients import _vendor, notion
from server.clients._checked_access import CheckedAccess
from server.knowledge.app import live_provider_search as live
from server.knowledge.domain.sources import KnowledgeSource
from tests.unit.clock_fake import ManualClock

_PAGE = "12345678-1234-1234-1234-123456789abc"
_DATABASE = "22345678-1234-1234-1234-123456789abc"
_OTHER_DATABASE = "32345678-1234-1234-1234-123456789abc"


def _source(kind: str, items: list[dict[str, Any]]) -> KnowledgeSource:
    return KnowledgeSource(
        org="org",
        namespace="ns",
        name="docs",
        kind=kind,
        source_id="selected-source",
        credential_mode="connected",
        access="mirrored",
        owner_subject="asker",
        connection_id="selected-generation",
        config={"include": ["docs"]} if kind == "gdrive" else {},
        items=items,
    )


def _access(kind: Literal["gdrive", "notion", "linear"]) -> CheckedAccess:
    async def validate() -> None:
        pass

    return CheckedAccess(
        "checked-token",
        kind=kind,
        deadline=asyncio.get_running_loop().time() + 60,
        validate=validate,
    )


async def test_drive_search_quotes_query_and_refuses_a_hit_moved_out_of_its_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("gdrive", [{"type": "filter", "in": "folder123456", "name_contains": "Plan"}])
    access = _access("gdrive")
    calls: list[httpx.Request] = []
    file = {
        "id": "file12345678",
        "name": "Plan",
        "trashed": False,
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["folder123456"],
        "webViewLink": "https://docs.google.com/document/d/file12345678/edit?private=1#heading",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer checked-token"
        if request.url.path.endswith("/files"):
            assert request.url.params["pageSize"] == "3"
            query = request.url.params["q"]
            assert "'folder123456' in parents" in query
            assert "name contains 'Plan'" in query
            assert query.endswith("fullText contains 'a\\' or trashed = true'")
            return httpx.Response(
                200, json={"files": [file, {**file, "id": "outside12345", "parents": []}]}
            )
        # A rename that still contains the fragment is outside Drive's prefix filter.
        return httpx.Response(200, json={**file, "name": "Old Plan"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(live, "get_client", lambda: client)
        hits = await live.search(
            source, access, "a' or trashed = true", top_k=3, deadline=access.deadline
        )
        assert hits == [
            live.LiveProviderHit(
                "file12345678",
                "Plan",
                "https://docs.google.com/document/d/file12345678/edit",
                "gdrive",
            )
        ]
        assert not await live.revalidate(source, access, hits[0], deadline=access.deadline)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("modified", "expected"),
    [
        ("2026-11-01T00:00:00Z", False),
        ("2026-11-01T00:00:00.000Z", False),
        ("2026-10-31T23:59:59.999999Z", False),
        ("2026-11-01T00:00:00.000001Z", True),
        ("2026-11-02T00:00:00Z", True),
        ("not-a-date", False),
        ("2026-13-99T00:00:00Z", False),
        ("2026-11-01T00:00:00", False),
    ],
)
async def test_drive_revalidation_excludes_the_strict_modification_cutoff(
    monkeypatch: pytest.MonkeyPatch, modified: str, expected: bool
) -> None:
    source = _source("gdrive", [{"type": "filter", "modified_after": "2026-11-01"}])
    access = _access("gdrive")
    hit = live.LiveProviderHit(
        "file12345678", "Plan", "https://docs.google.com/document/d/file12345678/edit", "gdrive"
    )
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.path.endswith("/files/" + hit.id)
        return httpx.Response(
            200,
            json={
                "id": hit.id,
                "name": hit.title,
                "mimeType": "application/vnd.google-apps.document",
                "trashed": False,
                "parents": [],
                "modifiedTime": modified,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(live, "get_client", lambda: client)
        assert await live.revalidate(source, access, hit, deadline=access.deadline) is expected
    assert len(calls) == 1


async def test_notion_search_filters_native_pages_and_rechecks_database_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("notion", [{"type": "database", "id": _DATABASE}])
    access = _access("notion")
    calls: list[httpx.Request] = []

    async def no_pacing(token: Any) -> None:
        pass

    page = {
        "object": "page",
        "id": _PAGE,
        "in_trash": False,
        "parent": {"type": "data_source_id", "database_id": _DATABASE},
        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Private title"}]}},
        "url": f"https://www.notion.so/{_PAGE}",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["Notion-Version"] == notion.API_VERSION
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {
                "query": "title",
                "page_size": 2,
                "filter": {"property": "object", "value": "page"},
            }
            return httpx.Response(
                200,
                json={"results": [page, {**page, "url": "https://notion.so.evil.example/private"}]},
            )
        return httpx.Response(
            200,
            json={**page, "parent": {"type": "data_source_id", "database_id": _OTHER_DATABASE}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(live, "get_client", lambda: client)
        monkeypatch.setattr(notion, "_pace", no_pacing)
        hits = await live.search(source, access, "title", top_k=2, deadline=access.deadline)
        assert len(hits) == 1 and hits[0].id == _PAGE and hits[0].title == "Private title"
        assert not await live.revalidate(source, access, hits[0], deadline=access.deadline)
    assert len(calls) == 2


async def test_an_expired_deadline_or_wrong_provider_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = _access("gdrive")
    source = _source("gdrive", [{"type": "folder", "id": "folder123456"}])
    clock = ManualClock(t=asyncio.get_running_loop().time())
    monkeypatch.setattr(live, "time", SimpleNamespace(monotonic=clock.monotonic))

    def no_client() -> Any:
        pytest.fail("a refused search must not acquire an HTTP client")

    monkeypatch.setattr(live, "get_client", no_client)
    with pytest.raises(live.SearchUnavailable):
        await live.search(_source("notion", []), access, "query", top_k=1, deadline=access.deadline)
    stop = clock.monotonic() + 1
    clock.advance(2)
    with pytest.raises(live.SearchUnavailable):
        await live.search(source, access, "query", top_k=1, deadline=stop)


class _Oversize(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False
        self.reads = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for piece in (b"x" * 65, b"must not be read"):
            self.reads += 1
            yield piece

    async def aclose(self) -> None:
        self.closed = True


async def test_live_response_cap_is_clamped_and_redirects_do_not_forward_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source("gdrive", [{"type": "folder", "id": "folder123456"}])
    access = _access("gdrive")
    stream = _Oversize()
    calls: list[httpx.Request] = []
    monkeypatch.setattr(_vendor.settings, "knowledge_source_sync_max_file_bytes", 64)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=stream)
        return httpx.Response(
            302,
            headers={"Location": "https://untrusted.example/collect"},
            json={
                "files": [
                    {
                        "id": "file12345678",
                        "name": "Redirect body must not answer",
                        "trashed": False,
                        "mimeType": "application/vnd.google-apps.document",
                        "parents": ["folder123456"],
                        "webViewLink": "https://docs.google.com/document/d/file12345678/edit",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        monkeypatch.setattr(live, "get_client", lambda: client)
        with pytest.raises(_vendor.TooLarge):
            await live.search(source, access, "query", top_k=1, deadline=access.deadline)
        assert stream.closed and stream.reads == 1
        monkeypatch.setattr(_vendor.settings, "knowledge_source_sync_max_file_bytes", 1024)
        with pytest.raises(_vendor.VendorError) as refused:
            await live.search(source, access, "query", top_k=1, deadline=access.deadline)
        assert refused.value.status == 302
    assert len(calls) == 2
    assert all(request.url.host == "www.googleapis.com" for request in calls)
