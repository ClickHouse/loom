"""A provider's 2xx answer is checked before it is stored, cached or ranked
(width, finiteness, one vector per batch slot), and an oversized text is the
caller's error, not a provider failure on /v1/health."""

from __future__ import annotations

import math

import pytest

from server.embedding import _cache
from server.embedding._provider import MalformedEmbedding, vector
from server.observability import provider_health as ph


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    async def post(self, url, *, json, headers):
        self.calls += 1
        return _Resp(self.payload)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    ph.reset()
    _cache._cache._store.clear()
    yield
    ph.reset()
    _cache._cache._store.clear()


def _openai(monkeypatch: pytest.MonkeyPatch, payload: dict, *, dim: int = 3):
    from server.embedding import openai as mod

    client = _Client(payload)
    monkeypatch.setattr(mod, "_http_client", lambda: client)
    monkeypatch.setattr(mod, "_raise_for_status", lambda resp, name: None)
    return mod.OpenAIEmbedder(model="text-embedding-3-small", dim=dim), client


def test_vector_refuses_empty_wrong_width_and_non_finite() -> None:
    assert vector([1, 0.5], 2) == [1.0, 0.5]
    assert vector([1.0], None) == [1.0]
    with pytest.raises(MalformedEmbedding, match="empty"):
        vector([], 3)
    with pytest.raises(MalformedEmbedding, match="2 dims"):
        vector([1.0, 2.0], 3)
    with pytest.raises(MalformedEmbedding, match="non-finite"):
        vector([1.0, math.nan], 2)
    with pytest.raises(MalformedEmbedding, match="non-finite"):
        vector([math.inf], 1)


async def test_openai_wrong_width_is_refused_and_marks_the_provider(monkeypatch) -> None:
    emb, _ = _openai(monkeypatch, {"data": [{"embedding": [0.1, 0.2]}], "usage": {}})
    with pytest.raises(MalformedEmbedding):
        await emb._embed_one_raw("hello")
    assert ph.status()["providers"]["openai:embeddings"]["failing"] is True
    assert _cache._cache._store == {}


async def test_openai_batch_with_a_repeated_index_fails_the_batch(monkeypatch) -> None:
    payload = {
        "data": [{"index": 0, "embedding": [1.0, 0, 0]}, {"index": 0, "embedding": [0, 1.0, 0]}],
        "usage": {},
    }
    emb, _ = _openai(monkeypatch, payload)
    with pytest.raises(MalformedEmbedding, match="batch slot"):
        await emb.embed_many(["a", "b"])


async def test_openai_ada_takes_whatever_width_it_emits(monkeypatch) -> None:
    from server.embedding import openai as mod

    client = _Client({"data": [{"embedding": [0.1, 0.2]}], "usage": {}})
    monkeypatch.setattr(mod, "_http_client", lambda: client)
    monkeypatch.setattr(mod, "_raise_for_status", lambda resp, name: None)
    emb = mod.OpenAIEmbedder(model="text-embedding-ada-002", dim=1536)
    assert await emb._embed_one_raw("hello") == [0.1, 0.2]


async def test_an_oversized_single_text_is_not_a_provider_failure(monkeypatch) -> None:
    from server.embedding._provider import EmbedderInputTooLarge

    emb, client = _openai(monkeypatch, {"data": [{"embedding": [1.0, 0, 0]}], "usage": {}})
    with pytest.raises(EmbedderInputTooLarge):
        await emb._embed_one_raw("word " * 20_000)
    assert client.calls == 0
    assert "openai:embeddings" not in ph.status()["providers"]


async def test_gemini_wrong_width_and_oversized_text(monkeypatch) -> None:
    from server.embedding import gemini as mod
    from server.embedding._provider import EmbedderInputTooLarge

    client = _Client({"embedding": {"values": [0.1, 0.2]}})
    monkeypatch.setattr(mod, "_http_client", lambda: client)
    monkeypatch.setattr(mod, "_raise_for_status", lambda resp, name: None)
    emb = mod.GeminiEmbedder(dim=3)
    with pytest.raises(EmbedderInputTooLarge):
        await emb._embed_one_raw("word " * 5_000)
    assert "gemini:embeddings" not in ph.status()["providers"]
    with pytest.raises(MalformedEmbedding):
        await emb._embed_one_raw("hello")
    assert ph.status()["providers"]["gemini:embeddings"]["failing"] is True
