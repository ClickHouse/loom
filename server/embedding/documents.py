"""Document embeddings for writes, including single-item and recovery paths."""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.embedding._provider import MalformedEmbedding, vector

if TYPE_CHECKING:
    from server.embedding.embedder import Embedder


async def document_vectors(embedder: Embedder, texts: list[str]) -> list[list[float]]:
    """Embed raw document text with one validated vector per input."""
    values = await embedder.embed_many(texts)
    if len(values) != len(texts):
        raise MalformedEmbedding(
            f"provider returned {len(values)} document vectors for {len(texts)} inputs"
        )
    return [vector(value, embedder.dim) for value in values]


async def document_vector(embedder: Embedder, text: str) -> list[float]:
    """A single stored document uses the same provider purpose as a batch."""
    return (await document_vectors(embedder, [text]))[0]
