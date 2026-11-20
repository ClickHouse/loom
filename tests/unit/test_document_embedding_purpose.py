"""Writes and write retries keep document vectors separate from query vectors."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.embedding._provider import MalformedEmbedding
from server.memory.app import chunk_projection, set_chunks, set_commit, update
from server.memory.app.reused_vectors import ReusedVectors


class PurposeEmbedder:
    model_name = "purpose-test"
    dim = 2

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.documents: list[list[str]] = []

    async def embed(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0, 0.0]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.documents.append(list(texts))
        return [[0.0, 2.0] for _ in texts]


@pytest.mark.asyncio
@pytest.mark.parametrize("key_text", ["", "configuration key"], ids=["value-only", "value-and-key"])
async def test_memory_set_embeds_document_axes(
    monkeypatch: pytest.MonkeyPatch, key_text: str
) -> None:
    raw = "  preserve\n  document whitespace  "
    monkeypatch.setattr(set_commit, "embedding_texts", lambda *a: (raw, raw, key_text))
    write = SimpleNamespace(
        input=SimpleNamespace(memory_type="fact", value=raw, memory_key="key"), serialized_value=raw
    )
    embedder = PurposeEmbedder()
    await set_commit._set_embed(write, embedder)
    assert write.embedding == [0.0, 2.0]
    assert write.key_embedding == ([0.0, 2.0] if key_text else [0.0, 0.0])
    assert embedder.queries == []
    assert embedder.documents == ([[raw, key_text]] if key_text else [[raw]])


@pytest.mark.asyncio
@pytest.mark.parametrize("key_text", ["", "configuration key"], ids=["value-only", "value-and-key"])
async def test_memory_update_embeds_document_axes(key_text: str) -> None:
    embedder = PurposeEmbedder()
    value, key = await update._embed_value_and_key(embedder, "value", key_text)
    assert value == [0.0, 2.0]
    assert key == ([0.0, 2.0] if key_text else [])
    assert embedder.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query_first", [True, False], ids=["query-first", "document-first"])
async def test_write_retry_cache_does_not_cross_embedding_purposes(query_first: bool) -> None:
    embedder = PurposeEmbedder()
    cached = ReusedVectors(embedder)
    if query_first:
        assert await cached.embed("same text") == [1.0, 0.0]
    assert await cached.embed_many(["same text", "same text"]) == [[0.0, 2.0], [0.0, 2.0]]
    assert await cached.embed("same text") == [1.0, 0.0]
    assert await cached.embed_many(["same text"]) == [[0.0, 2.0]]
    assert len(embedder.queries) == 1
    assert embedder.documents == [["same text"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target", [set_chunks, chunk_projection], ids=["set-chunks", "chunk-projection"]
)
async def test_serial_write_recovery_preserves_document_purpose(target: Any) -> None:
    class SingleDocumentOnly(PurposeEmbedder):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            if len(texts) > 1:
                raise RuntimeError("batch unavailable")
            return await super().embed_many(texts)

    embedder = SingleDocumentOnly()
    values = await target._embed_chunks(embedder, ["first", "second"])
    assert values == [[0.0, 2.0], [0.0, 2.0]]
    assert embedder.queries == []


@pytest.mark.asyncio
async def test_wrong_batch_cardinality_never_falls_back_to_query_vectors() -> None:
    class WrongCount(PurposeEmbedder):
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            return [[0.0, 2.0]]

    embedder = WrongCount()
    with pytest.raises(MalformedEmbedding):
        await update._embed_value_and_key(embedder, "value", "key")
    assert embedder.queries == []
