"""Collect ordinary search legs separately from their final ranking."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from server.core import ch, pg
from server.core.vectors.private_first import each
from server.knowledge.app import bm25 as knowledge_bm25
from server.knowledge.app import pg_serving, search

if TYPE_CHECKING:
    from server.knowledge.app.search import ChunkHit, HybridSearchQuery
    from server.knowledge.store.source_candidates import Leg


@dataclass
class LegPools:
    query: HybridSearchQuery
    pools: dict[Leg, list[ChunkHit]]
    top_k: int
    lexical_weight: float

    def ranked(self) -> list[ChunkHit]:
        if len(self.pools) == 1:
            return next(iter(self.pools.values()))
        return search.fuse_rrf(
            self.pools.get("semantic", []),
            self.pools.get("lexical", []),
            keyed=self.pools.get("keys", []),
            top_k=self.top_k,
            w_lex=self.lexical_weight,
            by_version=pg.enabled(),
        )

    async def serving(
        self, fallback: list[ChunkHit]
    ) -> tuple[dict[Leg, list[ChunkHit]], list[ChunkHit]]:
        """Filter ordinary heads before they can shadow a checked source copy."""
        ch.set_query_tag("knowledge.search.serving_legs")
        candidates = [*fallback, *(hit for pool in self.pools.values() for hit in pool)]
        kept, _dropped = await pg_serving.keep_served(self.query.org, candidates)
        alive = {id(hit) for hit in kept}
        out: dict[Leg, list[ChunkHit]] = {}
        for leg, pool in self.pools.items():
            seen = set()
            out[leg] = []
            for hit in pool:
                key = (hit.namespace, hit.source_uri, hit.chunk_idx)
                if id(hit) in alive and key not in seen:
                    seen.add(key)
                    out[leg].append(hit)
        return out, [hit for hit in fallback if id(hit) in alive]


async def collect(
    query: HybridSearchQuery,
    mode: Literal["semantic", "lexical", "hybrid"],
    *,
    lexical_weight: float,
) -> LegPools:
    """Preserve ordinary leg widths, cold-statistics behavior and fusion scales."""

    async def _read(call: Awaitable[Any]) -> Any:
        return await call

    top_k = query.top_k
    if mode == "semantic" or not query.tokens:
        if not query.keys:
            semantic = await search.hybrid_search(query)
            return LegPools(query, {"semantic": semantic}, top_k, lexical_weight)
        wide = replace(query, top_k=max(top_k * 3, 20))
        semantic, keyed = await each(_read)([search.hybrid_search(wide), search.keys_search(wide)])
        return LegPools(wide, {"semantic": semantic, "keys": keyed}, top_k, lexical_weight)
    avgdl, idfs = await knowledge_bm25.lexical_stats(
        ch.get_client(),
        org=query.org,
        namespaces=list(query.namespaces),
        embedding_model=query.embedding_model,
        tokens=query.tokens,
    )
    lexical_query = replace(query, idfs=idfs, avgdl=avgdl)
    if mode == "lexical":
        lexical = await search.lexical_search(lexical_query)
        return LegPools(lexical_query, {"lexical": lexical}, top_k, lexical_weight)
    wide = replace(lexical_query, top_k=max(top_k * 3, 20))
    semantic, lexical, keyed = await each(_read)(
        [search.hybrid_search(wide), search.lexical_search(wide), search.keys_search(wide)]
    )
    return LegPools(
        wide,
        {"semantic": semantic, "lexical": lexical, "keys": keyed},
        top_k,
        lexical_weight,
    )
