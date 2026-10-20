"""FakeNGramEmbedder: deterministic, network-free; the dev and test default.

Character trigrams of the lowercased text, each hashed (MD5) into one of
`dim` buckets, then L2-normalised. The same text gives the same vector in
every process; texts sharing trigrams score high cosine. It ranks nothing
like a real model, so production sets `LOOM_EMBEDDING_MODEL`.
"""

from __future__ import annotations

import hashlib
import math

from server.embedding.normalize import query_text
from server.llm import customer


class FakeNGramEmbedder:
    def __init__(self, *, dim: int = 1024) -> None:
        self.dim = dim
        self.model_name = f"fake-ngram-{dim}"

    async def embed(self, text: str) -> list[float]:
        # Queries are whitespace-normalised like the real providers';
        # `embed_many` (the write path) is raw. It checks the call's customer
        # as a metered provider would, so a path that names none fails in tests.
        await customer.admit()
        return self._embed_raw(query_text(text))

    def _embed_raw(self, text: str) -> list[float]:
        text = text.lower().strip()
        vec = [0.0] * self.dim
        if len(text) < 3:
            # Pad so a one- or two-character text still has a trigram.
            text = text + "   "
        for i in range(len(text) - 2):
            trigram = text[i : i + 3]
            bucket = (
                int.from_bytes(hashlib.md5(trigram.encode("utf-8")).digest()[:4], "big") % self.dim
            )
            vec[bucket] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One vector per raw text, in input order."""
        await customer.admit()
        return [self._embed_raw(t) for t in texts]
