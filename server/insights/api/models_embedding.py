"""Wire models of `embedding.embed`: the main Loom's embedder, served to its read-replicas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# One call's texts: a query is one, a write's chunks a batch.
MAX_TEXTS = 256
MAX_TEXT_CHARS = 32_000


class EmbeddingEmbedInput(BaseModel):
    # The texts to embed, in order; one vector comes back per text.
    texts: list[str] = Field(min_length=1, max_length=MAX_TEXTS)
    # `query` embeds each text as a search query (whitespace-normalised,
    # cached by text), `batch` as stored content (verbatim), the two paths an
    # embedder offers.
    mode: Literal["query", "batch"] = "query"


class EmbeddingEmbedOutput(BaseModel):
    # The model the vectors were made with; a caller that expects another refuses them.
    model: str
    dim: int
    vectors: list[list[float]]
