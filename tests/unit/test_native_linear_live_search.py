"""Native document hits stay inside the connected account's registered projects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from server.clients import linear
from server.clients._checked_access import CheckedAccess
from server.knowledge.app import live_provider_search as live
from server.knowledge.domain.sources import KnowledgeSource
from tests.unit.clock_fake import ManualClock

_DOCUMENT = "12345678-1234-1234-1234-123456789abc"
_PROJECT = "22345678-1234-1234-1234-123456789abc"
_OTHER_PROJECT = "32345678-1234-1234-1234-123456789abc"
_OTHER_DOCUMENT = "42345678-1234-1234-1234-123456789abc"
_URL = f"https://linear.app/workspace/document/plan-{_DOCUMENT}"


def _source() -> KnowledgeSource:
    return KnowledgeSource(
        org="org",
        namespace="ns",
        name="docs",
        kind="linear",
        source_id="selected-source",
        credential_mode="connected",
        access="mirrored",
        owner_subject="asker",
        connection_id="selected-generation",
        items=[{"type": "project", "id": _PROJECT}],
    )


def _access(validate: Callable[[], Awaitable[None]] | None = None) -> CheckedAccess:
    async def valid() -> None:
        pass

    return CheckedAccess(
        "checked-token",
        kind="linear",
        deadline=asyncio.get_running_loop().time() + 60,
        validate=validate or valid,
    )


def _document(**changes: Any) -> dict[str, Any]:
    return {
        "id": _DOCUMENT,
        "title": "Private plan",
        "url": _URL,
        "trashed": False,
        "archivedAt": None,
        "project": {"id": _PROJECT},
        **changes,
    }


def _response(nodes: Any) -> dict[str, Any]:
    return {
        "data": {
            "searchDocuments": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": True, "endCursor": "next-page-not-requested"},
            }
        }
    }


async def test_linear_search_uses_native_variables_and_only_registered_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, access = _source(), _access()
    calls: list[httpx.Request] = []
    query = 'plan") { document(id: "foreign") { id } } #'

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://api.linear.app/graphql"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer checked-token"
        body = json.loads(request.content)
        assert body["variables"] == {"term": query, "first": 2, "projects": [_PROJECT]}
        assert query not in body["query"]
        compact = " ".join(body["query"].split())
        assert "searchDocuments(" in compact
        assert "includeArchived: false" in compact
        assert "includeComments: false" in compact
        assert "project:" in compact and "in: $projects" in compact
        return httpx.Response(
            200,
            json=_response(
                [
                    _document(url=_URL + "?private=1#anchor"),
                    _document(project={"id": _OTHER_PROJECT}),
                ]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        hits = await live.search(source, access, query, top_k=2, deadline=access.deadline)
    assert hits == [live.LiveProviderHit(_DOCUMENT, "Private plan", _URL, "linear")]
    # A truncated provider page is not permission to widen or continue the search.
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"project": None},
        {"project": {}},
        {"project": {"id": _OTHER_PROJECT}},
        {"project": {"id": ["invalid"]}},
        {"trashed": True},
        {"trashed": None},
        {"trashed": 0},
        {"archivedAt": "2026-01-01T00:00:00Z"},
        {"archivedAt": False},
        {"id": "not-a-document-uuid"},
        {"url": "https://linear.app.evil.example/private"},
        {"url": "https://checked-token@linear.app/private"},
        {"url": "http://linear.app/private"},
    ],
)
async def test_linear_search_withholds_nonmembers_and_unsafe_links(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    access = _access()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=_response([_document(**changes)]))
        )
    ) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        assert await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline) == []


@pytest.mark.parametrize("missing", ["project", "trashed", "archivedAt"])
async def test_linear_membership_requires_explicit_current_state(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    access = _access()
    row = _document()
    del row[missing]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_response([row])))
    ) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        assert await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline) == []


@pytest.mark.parametrize(
    "data",
    [
        {"data": {}},
        {"data": {"searchDocuments": None}},
        _response({"id": _DOCUMENT}),
        _response([_document(), _document(id=_OTHER_DOCUMENT)]),
    ],
)
async def test_linear_search_refuses_malformed_or_overfull_pages(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]
) -> None:
    access = _access()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data))
    ) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        with pytest.raises(live.SearchUnavailable):
            await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline)


@pytest.mark.parametrize("with_partial_data", [False, True])
async def test_linear_graphql_errors_never_release_partial_hits(
    monkeypatch: pytest.MonkeyPatch, with_partial_data: bool
) -> None:
    access = _access()
    body = _response([_document()]) if with_partial_data else {}
    body["errors"] = [{"message": "private upstream detail", "extensions": {"code": "FORBIDDEN"}}]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        with pytest.raises(live.SearchUnavailable) as refused:
            await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline)
        assert "private upstream detail" not in str(refused.value)
        assert _DOCUMENT not in str(refused.value)
        assert not await live.revalidate(
            _source(),
            access,
            live.LiveProviderHit(_DOCUMENT, "Plan", _URL, "linear"),
            deadline=access.deadline,
        )


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


async def test_linear_stream_cap_and_redirect_refusal_do_not_release_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = _access()
    stream = _Oversize()
    calls: list[httpx.Request] = []
    monkeypatch.setattr(live.settings, "knowledge_live_search_response_max_bytes", 64)

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=stream)
        return httpx.Response(302, headers={"Location": "https://untrusted.example/collect"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        with pytest.raises(live.SearchUnavailable):
            await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline)
        assert stream.closed and stream.reads == 1
        with pytest.raises(live.SearchUnavailable) as refused:
            await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline)
        assert "untrusted.example" not in str(refused.value)
    assert len(calls) == 2
    assert all(request.url.host == "api.linear.app" for request in calls)


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"title": "New title", "url": "https://linear.app/new-location"}, True),
        ({"id": _OTHER_DOCUMENT}, False),
        ({"project": {"id": _OTHER_PROJECT}}, False),
        ({"project": None}, False),
        ({"trashed": True}, False),
        ({"archivedAt": "2026-01-01T00:00:00Z"}, False),
    ],
)
async def test_linear_revalidation_checks_current_document_without_replacing_the_hit(
    monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any], expected: bool
) -> None:
    access = _access()
    hit = live.LiveProviderHit(_DOCUMENT, "Original title", _URL, "linear")
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        assert body["variables"] == {"id": _DOCUMENT}
        assert "document(id: $id)" in " ".join(body["query"].split())
        assert request.headers["Authorization"] == "Bearer checked-token"
        return httpx.Response(200, json={"data": {"document": _document(**changes)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        assert await live.revalidate(_source(), access, hit, deadline=access.deadline) is expected
    assert len(calls) == 1
    assert hit == live.LiveProviderHit(_DOCUMENT, "Original title", _URL, "linear")


async def test_linear_revocation_at_the_response_boundary_withholds_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revoked = False
    calls: list[httpx.Request] = []

    async def validate() -> None:
        if revoked:
            raise PermissionError("checkout revoked")

    access = _access(validate)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal revoked
        calls.append(request)
        revoked = True
        return httpx.Response(200, json=_response([_document()]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        with pytest.raises(PermissionError, match="checkout revoked"):
            await live.search(_source(), access, "plan", top_k=1, deadline=access.deadline)
        with pytest.raises(PermissionError, match="checkout revoked"):
            await live.revalidate(
                _source(),
                access,
                live.LiveProviderHit(_DOCUMENT, "Plan", _URL, "linear"),
                deadline=access.deadline,
            )
    assert len(calls) == 1


async def test_linear_cancellation_during_transport_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()
    access = _access()

    async def handle(_: httpx.Request) -> httpx.Response:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("cancelled transport must not produce a result")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(linear, "get_client", lambda: client)
        task = asyncio.create_task(
            live.search(_source(), access, "plan", top_k=1, deadline=access.deadline)
        )
        started = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait({task, started}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                pytest.fail("native search returned before entering the blocked transport")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)


async def test_linear_expired_deadline_and_nonproject_scope_never_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = _access()
    clock = ManualClock(t=asyncio.get_running_loop().time())
    monkeypatch.setattr(live, "time", SimpleNamespace(monotonic=clock.monotonic))

    def no_client() -> Any:
        pytest.fail("a refused native search must not acquire a client")

    monkeypatch.setattr(linear, "get_client", no_client)
    deadline = clock.monotonic() + 1
    clock.advance(2)
    with pytest.raises(live.SearchUnavailable):
        await live.search(_source(), access, "plan", top_k=1, deadline=deadline)
    assert (
        await live.search(
            replace(_source(), items=[{"type": "project", "slug_id": "123456789abc"}]),
            access,
            "plan",
            top_k=1,
            deadline=access.deadline,
        )
        == []
    )
