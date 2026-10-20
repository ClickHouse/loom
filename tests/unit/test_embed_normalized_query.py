"""The query is embedded as the text the cache key already
normalises. One whitespace normaliser; the read side (single-text `embed`)
sends it to the provider, the write side (`embed_many`) stays raw; the cache
key is salted so rows keyed before the query text was normalised never serve
it."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest

import server.embedding._cache as ec
from server.embedding.fake import FakeNGramEmbedder
from server.embedding.normalize import normalize_whitespace, query_text
from server.settings import settings


@pytest.fixture(autouse=True)
def _one_orgs_request() -> Iterator[None]:
    """The caches are scoped by the request's org; outside one they are skipped."""
    from server.observability import genai

    genai.set_identity(org="acme", namespace="ns", operation="memory.search")
    yield
    genai._identity.set(None)


RAW = "src Storages  MergeTree\n\nClaimed changelog\tcategory:  Bug Fix "
NORM = "src Storages MergeTree Claimed changelog category: Bug Fix"


def test_normaliser_collapses_whitespace_only() -> None:
    assert normalize_whitespace(RAW) == NORM
    # no case folding, no punctuation changes
    assert normalize_whitespace("A  b, C!") == "A b, C!"
    assert normalize_whitespace("") == ""


def test_one_function_everywhere() -> None:
    assert ec._normalize is normalize_whitespace


@pytest.mark.asyncio
async def test_cached_embed_sends_the_normalised_text(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_cache_shared", False)
    ec._cache._store.clear()
    seen: list[str] = []

    async def fetch(text: str) -> list[float]:
        seen.append(text)
        return [1.0]

    vec, hit = await ec.cached_embed("m", RAW, fetch, dim=1)
    assert seen == [NORM] and hit is False
    # the other spelling is the same key: served from the process tier
    vec2, hit2 = await ec.cached_embed("m", NORM, fetch, dim=1)
    assert hit2 is True and vec2 == vec and seen == [NORM]


def test_key_is_salted_and_shared_across_spellings() -> None:
    # The layout loom.embedding_cache rows are keyed by: changing it orphans
    # every shared-tier row.
    h = hashlib.sha1()
    h.update(b"org\0acme\0m\0ws1\0")
    h.update(NORM.encode())
    assert ec._key("m", RAW, "acme") == h.hexdigest()
    unsalted = hashlib.sha1(b"org\0acme\0m\0" + NORM.encode()).hexdigest()
    assert ec._key("m", RAW, "acme") != unsalted
    assert ec._key("m", RAW, "acme") != ec._key("m", RAW, "beta")  # one org's own
    spellings = (RAW, NORM, "  " + NORM + "\n")
    assert len({ec._key("m", t, "acme") for t in spellings}) == 1


@pytest.mark.asyncio
async def test_fake_embedder_reads_normalised_writes_raw() -> None:
    emb = FakeNGramEmbedder(dim=256)
    assert await emb.embed(RAW) == await emb.embed(NORM)
    # the write path (embed_many) is raw
    assert (await emb.embed_many([NORM]))[0] == await emb.embed(RAW)
    assert (await emb.embed_many([RAW]))[0] != await emb.embed(RAW)


def test_query_text_is_the_normalised_text() -> None:
    assert query_text(RAW) == NORM


@pytest.mark.asyncio
async def test_batch_and_query_never_share_a_process_cache_entry(monkeypatch) -> None:
    """A document embedded raw by `embed_many` must not answer a query that
    normalises to the same text, nor the reverse: each path's vector is of
    the text it sends."""
    from server.embedding import openai as mod

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "embedding_cache_shared", False)
    ec._cache._store.clear()
    sent: list[str] = []

    class _Resp:
        def json(self) -> dict:
            return self.body

    class _Client:
        async def post(self, url, json, headers):
            inputs = json["input"] if isinstance(json["input"], list) else [json["input"]]
            sent.extend(inputs)
            r = _Resp()
            r.body = {
                "data": [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(inputs)],
                "usage": {"total_tokens": 1},
            }
            return r

    monkeypatch.setattr(mod, "_http_client", lambda: _Client())
    monkeypatch.setattr(mod, "_raise_for_status", lambda resp, name: None)
    emb = mod.OpenAIEmbedder(dim=1)

    assert await emb.embed_many([RAW]) == [[float(len(RAW))]]
    assert await emb.embed(NORM) == [float(len(NORM))]
    assert sent == [RAW, NORM]
    # a repeated batch text is still a process-cache hit
    assert await emb.embed_many([RAW]) == [[float(len(RAW))]] and sent == [RAW, NORM]
