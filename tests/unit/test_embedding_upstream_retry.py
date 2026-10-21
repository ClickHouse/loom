"""An upstream 5xx or 429 from the embedding provider is
retried like a dropped connection; other 4xx are not."""

from __future__ import annotations

import httpx
import pytest

from server.llm.customer import Customer


class _Resp:
    def __init__(self, code: int, payload: dict) -> None:
        self.status_code = code
        self._payload = payload
        self.request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, replies: list[_Resp]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def post(self, url, *, json, headers):
        self.calls += 1
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_openai_embedder_survives_one_500(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.embedding import openai as mod

    ok = {"data": [{"embedding": [0.1, 0.2, 0.3]}], "usage": {"total_tokens": 3}}
    client = _Client([_Resp(500, {}), _Resp(200, ok)])
    monkeypatch.setattr(mod, "_http_client", lambda: client)
    import tenacity

    monkeypatch.setattr("server.clients._retry.wait_exponential", lambda **_: tenacity.wait_none())
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = mod.OpenAIEmbedder(model="text-embedding-3-small", dim=3)
    vec = await emb._embed_one_raw_inner("hello", Customer())
    assert vec == [0.1, 0.2, 0.3] and client.calls == 2


@pytest.mark.asyncio
async def test_openai_embedder_does_not_retry_a_400(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.embedding import openai as mod

    client = _Client([_Resp(400, {}), _Resp(200, {})])
    monkeypatch.setattr(mod, "_http_client", lambda: client)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = mod.OpenAIEmbedder(model="text-embedding-3-small", dim=3)
    with pytest.raises(httpx.HTTPStatusError):
        await emb._embed_one_raw_inner("hello", Customer())
    assert client.calls == 1


@pytest.mark.asyncio
async def test_a_failed_batch_marks_the_provider_failing_and_a_cancel_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch path (the embedding worker's) reports to `/v1/health` like
    the single path; a cancelled call (a query-embed deadline) is not the
    provider's failure."""
    import asyncio

    from server.embedding import _cache
    from server.embedding import openai as mod
    from server.observability import provider_health as ph

    ph.reset()
    _cache._cache._store.clear()
    monkeypatch.setattr(mod, "_http_client", lambda: _Client([_Resp(401, {})]))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    emb = mod.OpenAIEmbedder(model="text-embedding-3-small", dim=3)
    with pytest.raises(httpx.HTTPStatusError):
        await emb.embed_many(["a"])
    assert ph.status()["providers"]["openai:embeddings"]["failing"] is True

    ph.reset()

    class _Hang:
        async def post(self, url, *, json, headers):
            await asyncio.sleep(10)

    monkeypatch.setattr(mod, "_http_client", lambda: _Hang())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(emb._embed_one_raw("b"), timeout=0.01)
    assert "openai:embeddings" not in ph.status()["providers"]
