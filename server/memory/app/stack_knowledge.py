"""Checked semantic knowledge for a stack, with complete ordinary selections."""

from __future__ import annotations

from dataclasses import dataclass

from server import embedding
from server.core.errors import VectorCapacityError
from server.knowledge.app import pg_serving, search
from server.knowledge.app.knowledge_search import not_noise
from server.knowledge.app.search import ChunkHit, HybridSearchQuery
from server.knowledge.app.search_legs import LegPools
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_search_passes import SearchPasses
from server.knowledge.app.source_search_proofs import SearchProofs
from server.memory.app.agent_rules import request_rule_agent, served_hits
from server.memory.domain.assistant_rules import hits_to_ask


@dataclass
class StackKnowledge:
    hits: list[ChunkHit]
    _ordinary: list[ChunkHit]
    proofs: SearchProofs | None
    lost: bool = False

    def ordinary(self) -> list[ChunkHit]:
        if self.proofs is not None:
            self.proofs, self.lost = None, True
            self.hits = self._ordinary
        return self.hits

    def fresh(self) -> bool:
        return self.proofs is None or self.proofs.fresh()

    async def current(self) -> list[ChunkHit]:
        if self.proofs is not None:
            proof = self.proofs
            try:
                await proof.check()
                if not proof.fresh():
                    raise LostCorpus()
            except LostCorpus:
                return self.ordinary()
        return self.hits


async def _select(
    hits: list[ChunkHit], wider: list[ChunkHit] | None, agent: str, top_k: int
) -> tuple[list[ChunkHit], bool]:
    used_wide = False

    async def saved(_width: int) -> list[ChunkHit]:
        nonlocal used_wide
        used_wide = True
        if wider is None:
            raise LostCorpus()
        return wider

    selected = await served_hits(hits, saved, lambda hit: hit.tags, agent, top_k, keep=not_noise())
    return selected, used_wide


async def collect(
    org: str, namespace: str, agent: str, seed: str, top_k: int, plan: ReadPlan
) -> StackKnowledge:
    """Keep semantic widths distinct; marked readers collect each width once."""
    if plan.org != org:
        raise ValueError("document read scope changed")
    embedder = embedding.get_embedder()
    vector = await embedder.embed(seed)
    agent = request_rule_agent(agent)

    def query(width: int) -> HybridSearchQuery:
        return HybridSearchQuery(org, (namespace,), embedder.model_name, vector, width)

    captured: list[LegPools] = []

    async def ordinary(width: int) -> list[ChunkHit]:
        async def ranked(size: int) -> list[ChunkHit]:
            actual = query(size)
            hits = await search.hybrid_search(actual)
            captured[:] = [LegPools(actual, {"semantic": hits}, size, 1.0)]
            return hits

        return (await pg_serving.served_search(org, ranked, width))[0]

    async def collect_pass(width: int, fallback: list[ChunkHit]) -> None:
        legs = captured[0]
        serving, fallback = await legs.serving(fallback)
        await passes.collect(
            legs.query, serving, legs=("semantic",), fallback=fallback, top_k=width
        )

    first = await ordinary(top_k)
    if not plan.marked:
        selected = await served_hits(
            first, ordinary, lambda hit: hit.tags, agent, top_k, keep=not_noise()
        )
        return StackKnowledge(selected, selected, None)
    passes = SearchPasses(plan)
    await collect_pass(top_k, first)
    width = hits_to_ask(top_k)
    wider = None
    if width > top_k:
        try:
            wider = await ordinary(width)
            await collect_pass(width, wider)
        except VectorCapacityError:
            raise
        except Exception:  # noqa: BLE001 — a wider ordinary failure must preserve the usable first pass
            wider = None
    pools = await passes.finish()
    mixed = [[ranked.chunk for ranked in pool] for pool in pools]
    fallback = [[ranked.chunk for ranked in pool] for pool in passes.ordinary()]
    mixed_wide = mixed[1] if len(mixed) > 1 else None
    ordinary_wide = fallback[1] if len(fallback) > 1 else None
    output, used_wide = await _select(mixed[0], mixed_wide, agent, top_k)
    passes.restrict_proofs(pools[:2] if used_wide else pools[:1])
    baseline, _ = await _select(fallback[0], ordinary_wide, agent, top_k)
    result = StackKnowledge(output, baseline, passes.proofs if passes.proofs.mirrored else None)
    await result.current()
    return result
