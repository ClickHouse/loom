"""An embedder that remembers each text's vector, for a write that starts over under
the Postgres authority (`memory.set`, `memory.bulk_set`): no text is embedded twice."""

from __future__ import annotations

from typing import Any


class ReusedVectors:
    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder
        self._known: dict[str, list[float]] = {}
        self.model_name = embedder.model_name
        self.dim = embedder.dim

    async def embed(self, text: str) -> list[float]:
        if text not in self._known:
            self._known[text] = await self._embedder.embed(text)
        return self._known[text]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        missing = list(dict.fromkeys(t for t in texts if t not in self._known))
        if missing:
            vectors = await self._embedder.embed_many(missing)
            self._known.update(zip(missing, vectors, strict=True))
        return [self._known[t] for t in texts]
