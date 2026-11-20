"""Feed requests stay on the API origin and never accept partial provider pages."""

import json
from datetime import UTC, datetime

import httpx
import pytest

from server.clients import github_feed as client


@pytest.fixture
def transport(monkeypatch):
    calls = []
    replies = []

    def handle(request):
        calls.append(request)
        return replies.pop(0)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(client, "get_client", lambda: http)
    return calls, replies, http


@pytest.mark.asyncio
async def test_pages_use_separate_streams_and_exclusive_since_overlap(transport):
    calls, replies, http = transport
    replies.extend([httpx.Response(200, json=[{"id": 1}]), httpx.Response(200, json=[])])
    try:
        batch = await client.pages(
            "widgets/repo",
            "secret",
            "review_comments",
            datetime(2025, 1, 2, tzinfo=UTC),
            max_pages=2,
        )
        assert batch.complete and batch.items == [{"id": 1}]
        assert str(calls[0].url).startswith(
            "https://api.github.com/repos/widgets/repo/pulls/comments?"
        )
        assert calls[0].url.params["since"] == "2025-01-01T23:59:59Z"
        assert calls[0].headers["authorization"] == "Bearer secret"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_full_page_reports_incomplete_boundary(transport):
    calls, replies, http = transport
    replies.extend(
        httpx.Response(200, json=[{"id": n} for n in range(start, start + 100)])
        for start in (0, 100)
    )
    try:
        batch = await client.pages("widgets/repo", "secret", "items", None, max_pages=2)
        assert not batch.complete and len(batch.items) == 200
        assert calls[0].url.params["state"] == "all"
    finally:
        await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
async def test_refusal_never_logs_body_or_follows_redirect(transport, status):
    calls, replies, http = transport
    replies.append(
        httpx.Response(
            status, text="private-canary-secret", headers={"location": "https://evil.example"}
        )
    )
    try:
        with pytest.raises(client.FeedProviderError) as error:
            await client.metadata("widgets/repo", "secret")
        assert str(status) in str(error.value)
        assert "private-canary" not in str(error.value) and len(calls) == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_bad_list_entry_refuses_whole_page(transport):
    _, replies, http = transport
    replies.append(httpx.Response(200, json=[{"id": 1}, "bad"]))
    try:
        with pytest.raises(client.FeedProviderError):
            await client.pages("widgets/repo", "secret", "items", None, max_pages=2)
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_size_bound_is_not_bypassed_by_json(transport, monkeypatch):
    _, replies, http = transport
    monkeypatch.setattr(client, "PAGE_MAX_BYTES", 32)
    replies.append(httpx.Response(200, content=json.dumps({"data": "x" * 64}).encode()))
    try:
        with pytest.raises(client.FeedProviderError):
            await client.metadata("widgets/repo", "secret")
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_one_page_budget_refuses_before_request(transport):
    calls, _, http = transport
    try:
        with pytest.raises(client.FeedProviderError, match="2..100 max-pages"):
            await client.pages("widgets/repo", "secret", "items", None, max_pages=1)
        assert not calls
    finally:
        await http.aclose()
