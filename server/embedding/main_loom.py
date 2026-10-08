"""A read-replica's embedder: the main Loom's, over `embedding.embed`.

Chosen when a read-replica knows the main Loom's URL and has no key for the
provider the deployment's model needs (`embeds_via_main`), so a replica runs
with no provider key. The vectors are the main Loom's model's, cached under
the same model name, so the shared embedding cache serves both.
"""

from __future__ import annotations

from server.embedding._cache import cached_embed
from server.embedding._provider import MalformedEmbedding, vector

# Texts per `embedding.embed` call on the batch path.
_BATCH = 64


class MainLoomEmbedder:
    def __init__(self, *, model: str, dim: int) -> None:
        self.model_name = model
        self.dim = dim

    async def _request(self, texts: list[str], mode: str) -> list[list[float]]:
        from server.clients import main_loom

        data = await main_loom.call("embedding.embed", {"texts": texts, "mode": mode})
        if str(data.get("model") or "") != self.model_name:
            raise MalformedEmbedding(
                f"the main Loom embeds with {data.get('model')!r}, this replica expects "
                f"{self.model_name!r}"
            )
        rows = data.get("vectors")
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise MalformedEmbedding("embedding.embed answered another count than it was sent")
        return [vector(row, self.dim) for row in rows]

    async def _embed_one_raw(self, text: str) -> list[float]:
        return (await self._request([text], "query"))[0]

    async def embed(self, text: str) -> list[float]:
        result, _ = await cached_embed(self.model_name, text, self._embed_one_raw, dim=self.dim)
        return result

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for offset in range(0, len(texts), _BATCH):
            out.extend(await self._request(texts[offset : offset + _BATCH], "batch"))
        return out
