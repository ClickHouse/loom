"""Every failed GitHub read is a GitHubError, a timeout included.

Callers (the code_pr_history worker, the `loom code backfill-*` commands)
stop or skip on `GitHubError` with their cursor unmoved; an exception of any
other type escaped that handling and ended the whole pass.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from server.clients import github


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_timeout_is_a_github_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    with pytest.raises(github.GitHubError) as ei:
        await github.pr_files("o/r", "tok", 1)
    assert not isinstance(ei.value, github.RateLimited)
    assert "ReadTimeout" in str(ei.value)


@pytest.mark.asyncio
async def test_non_json_body_is_a_github_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    with pytest.raises(github.GitHubError, match="not JSON"):
        await github.repo_fork_upstreams("o/r", "tok")


@pytest.mark.asyncio
async def test_rate_limit_still_raises_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, text="{}")

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    with pytest.raises(github.RateLimited):
        await github.pr_files("o/r", "tok", 1)


@pytest.mark.asyncio
async def test_items_walk_keeps_pages_before_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    page_one = [
        {"number": i, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}
        for i in range(1, 3)
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "1":
            return httpx.Response(200, json=page_one)
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    items = await github.github_items_since("o/r", "tok", None, per_page=2, pace_s=0)
    assert [i.number for i in items] == [1, 2]


@pytest.mark.asyncio
async def test_a_list_endpoint_answering_an_object_is_a_github_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy's or an API's error object on a 200 is skipped, not read with
    `.get` on each dict key (AttributeError) to end the pass."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "Moved Permanently"})

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    with pytest.raises(github.GitHubError, match="expected a JSON array"):
        await github.pr_files("o/r", "tok", 1)
    with pytest.raises(github.GitHubError, match="expected a JSON array"):
        await github.merged_prs_since("o/r", "tok", None)


@pytest.mark.asyncio
async def test_an_object_endpoint_answering_a_list_is_a_github_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    with pytest.raises(github.GitHubError, match="expected a JSON object"):
        await github.pr_head_and_base("o/r", "tok", 7)
    with pytest.raises(github.GitHubError, match="expected a JSON object"):
        await github.merged_prs_window("o/r", "tok", "2026-01-01")


@pytest.mark.asyncio
async def test_list_items_that_are_not_objects_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"filename": "a.py"}, "junk", None, {"filename": "b.py"}])

    monkeypatch.setattr(github, "get_client", lambda: _client(handler))
    assert await github.pr_files("o/r", "tok", 1) == ["a.py", "b.py"]
