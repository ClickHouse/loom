"""The shared provider client never carries a key header to another host."""

from __future__ import annotations

import httpx
import pytest

from server.clients import _http


def _client(seen: list[httpx.Request], location: str) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(200, json={})

    return _http._ProviderClient(transport=httpx.MockTransport(handler), follow_redirects=True)


@pytest.mark.asyncio
async def test_key_headers_are_dropped_on_a_cross_origin_redirect() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen, "https://elsewhere.test/v1/messages") as c:
        await c.post(
            "https://api.provider.test/v1/messages",
            headers={"x-api-key": "k1", "x-goog-api-key": "k2", "Authorization": "Bearer t"},
        )
    hop = seen[1].headers
    assert seen[1].url.host == "elsewhere.test"
    assert "x-api-key" not in hop and "x-goog-api-key" not in hop and "authorization" not in hop


@pytest.mark.asyncio
async def test_key_headers_survive_a_same_origin_redirect() -> None:
    seen: list[httpx.Request] = []
    async with _client(seen, "/v1/messages/") as c:
        await c.post("https://api.provider.test/v1/messages", headers={"x-api-key": "k1"})
    assert seen[1].headers["x-api-key"] == "k1"


def test_the_shared_client_is_the_stripping_one() -> None:
    assert isinstance(_http.get_client(), _http._ProviderClient)
