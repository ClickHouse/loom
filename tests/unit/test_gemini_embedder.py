"""Unit tests for GeminiEmbedder — HTTP fully mocked, no network.

Covers the pieces that differ from OpenAIEmbedder: response parsing
(`embedding.values` / `embeddings[].values`), the `x-goog-api-key` header,
`outputDimensionality` in the request, positional batch mapping, the
size-mismatch hard failure, and the 2048-token size guard.
"""

from __future__ import annotations

import pytest

from server.embedding import _cache as cache_mod
from server.embedding.gemini import _MAX_INPUT_TOKENS, GeminiEmbedder
from server.embedding.openai import EmbedderInputTooLarge


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Records every post; replies from a queued list of payloads."""

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    async def post(self, url, *, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(self._payloads.pop(0))


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    # A key so the constructor doesn't raise; the client is mocked anyway.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    cache_mod._cache._store.clear()
    yield
    cache_mod._cache._store.clear()


def _wire(monkeypatch: pytest.MonkeyPatch, payloads: list[dict]) -> _FakeClient:
    client = _FakeClient(payloads)
    monkeypatch.setattr("server.embedding.gemini._http_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_embed_single_parses_values_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _wire(monkeypatch, [{"embedding": {"values": [0.1, 0.2, 0.3]}}])
    emb = GeminiEmbedder(model="gemini-embedding-001", dim=3)
    vec = await emb.embed("hello world")
    assert vec == [0.1, 0.2, 0.3]
    call = client.calls[0]
    # Auth via header, never the URL query param.
    assert call["headers"]["x-goog-api-key"] == "test-key-123"
    assert "key=" not in call["url"]
    assert call["url"].endswith("gemini-embedding-001:embedContent")
    assert call["json"]["outputDimensionality"] == 3
    assert call["json"]["content"]["parts"][0]["text"] == "hello world"


@pytest.mark.asyncio
async def test_embed_many_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"embeddings": [{"values": [1.0]}, {"values": [2.0]}, {"values": [3.0]}]}
    client = _wire(monkeypatch, [payload])
    emb = GeminiEmbedder(dim=1)
    out = await emb.embed_many(["a", "b", "c"])
    assert out == [[1.0], [2.0], [3.0]]
    # One batch call, batchEmbedContents, one request per text in order.
    assert client.calls[0]["url"].endswith("gemini-embedding-001:batchEmbedContents")
    reqs = client.calls[0]["json"]["requests"]
    assert [r["content"]["parts"][0]["text"] for r in reqs] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_batch_size_mismatch_is_hard_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two inputs, provider returns one embedding — must raise, not misalign.
    _wire(monkeypatch, [{"embeddings": [{"values": [1.0]}]}])
    emb = GeminiEmbedder(dim=1)
    with pytest.raises(RuntimeError, match="batch size mismatch"):
        await emb.embed_many(["a", "b"])


@pytest.mark.asyncio
async def test_oversized_input_rejected_before_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _wire(monkeypatch, [{"embedding": {"values": [0.0]}}])
    emb = GeminiEmbedder(dim=1)
    huge = "x " * (_MAX_INPUT_TOKENS * 5)  # well over 2048 tokens
    with pytest.raises(EmbedderInputTooLarge):
        await emb.embed(huge)
    assert client.calls == []  # never hit the provider


def test_missing_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiEmbedder()
