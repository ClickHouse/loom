"""Facts client. Query the typed `(subject, relation, object)` triples
the extractor emits alongside memories.

    facts = agent.facts.list(entity="alice")
    for row in facts.rows:
        print(f"{row.subject} {row.relation} {row.object}  (×{row.co_occur})")

    neighbors = await agent.facts.related(entity="alice", top_k=10)
    for r in neighbors.related:
        print(f"{r.entity}  ×{r.co_occur} ({r.role})")

The data is populated by `memory.set_from_messages`: its extractor
emits a triple when the source text asserts a verb between named
things ("user works at acme" -> ("user","works_at","acme")).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from loom.agent import Agent


class FactsClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def list(
        self,
        *,
        subject: str | None = None,
        object: str | None = None,
        relation: str | None = None,
        entity: str | None = None,
        limit: int = 50,
        sort_by: str = "co_occur",
        include_sources: bool = False,
        min_co_occur: int = 1,
    ) -> dict[str, Any]:
        """List `(subject, relation, object)` triples in this namespace.

        Filters AND together. `entity` is a convenience that ORs across
        subject and object — pass it when you want "all facts about X"
        regardless of direction. `include_sources=True` adds a fan-out
        to each triple's source memory_keys (up to 10 per triple).

        Returns `{"rows": [{subject, relation, object, co_occur,
        last_seen, source_keys}, ...], "truncated": bool}`.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "limit": limit,
            "sort_by": sort_by,
            "include_sources": include_sources,
            "min_co_occur": min_co_occur,
        }
        if subject is not None:
            payload["subject"] = subject
        if object is not None:
            payload["object"] = object
        if relation is not None:
            payload["relation"] = relation
        if entity is not None:
            payload["entity"] = entity
        return await self._agent.transport.post("/v1/facts.list", payload)

    async def related(
        self,
        entity: str,
        *,
        top_k: int = 20,
    ) -> dict[str, Any]:
        """Entities that co-occur with `entity` via any relation, ranked
        by total co_occur DESC.

        Co-occurrence is walked in both directions; each neighbor
        carries `role: "subject"|"object"|"both"` so callers can tell
        "alice WORKS_AT acme" (acme is object) from "acme EMPLOYS alice"
        (alice is object).

        Returns `{"entity": <entity canonicalised like a write>, "related":
        [{entity, co_occur, role}, ...]}`; a name with no canonical form returns
        `entity` `""` and no neighbours.
        """
        return await self._agent.transport.post(
            "/v1/facts.related",
            {
                "org": self._agent.org,
                "namespace": self._agent.namespace,
                "entity": entity,
                "top_k": top_k,
            },
        )

    async def traverse(
        self,
        seed_entity: str,
        *,
        max_hops: int = 2,
        top_k_per_hop: int = 20,
        min_co_occur: int = 1,
        as_of: datetime | None = None,
        labeled: bool = False,
        relations: Sequence[str] | None = None,
        hub_degree: int | None = None,
        hub_share: float | None = None,
        rank_by: str | None = None,
    ) -> dict[str, Any]:
        """Walk the entity co-occurrence graph N hops out from a seed.

        Frontier expansion, bidirectional neighbor union, cycle dedup,
        and cumulative weighting happen server-side in a single
        round-trip. `as_of` walks only edges valid at that instant;
        `labeled` / `relations` switch to the typed triple graph.
        `hub_degree` reports an entity whose total
        co-occurrence degree is at or above it but does not expand it;
        `hub_share` does the same for an entity the
        reaching edge explains at most that share of (`via_co_occur /
        degree`). Such nodes carry `hub: true` and are listed in `hubs`.
        None = the server default; the seed always expands. `rank_by` is
        what each hop's top-K trim orders by:
        "co_occur" (most co-mentions), "specificity" (co-mentions as a
        share of the neighbour's degree) or "cosine"; None = server default.

        Returns `{"seed": <echoed>, "nodes": [{entity, hop,
        cumulative_weight, via_co_occur, hub, path}], "hubs": [...]}`
        where `path` is the chain from the seed to the node, seed first.
        Nodes are ordered by hop ASC, then cumulative_weight DESC.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "seed_entity": seed_entity,
            "max_hops": max_hops,
            "top_k_per_hop": top_k_per_hop,
            "min_co_occur": min_co_occur,
            "labeled": labeled,
        }
        if as_of is not None:
            payload["as_of"] = as_of.isoformat()
        if relations:
            payload["relations"] = list(relations)
        if hub_degree is not None:
            payload["hub_degree"] = int(hub_degree)
        if hub_share is not None:
            payload["hub_share"] = float(hub_share)
        if rank_by is not None:
            payload["rank_by"] = str(rank_by)
        return await self._agent.transport.post("/v1/facts.traverse", payload)

    async def path(
        self,
        entity_a: str,
        entity_b: str,
        *,
        max_hops: int = 3,
        top_k_per_hop: int = 20,
        min_co_occur: int = 1,
        as_of: datetime | None = None,
        labeled: bool = False,
        relations: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """How two entities are connected: the shortest chain of edges from
        `entity_a` to `entity_b` within `max_hops` (TOTAL path length), found
        by a bidirectional search that meets in the middle.

        Returns `{"found", "path": [entity_a, ..., entity_b], "edges":
        [{entity_a, entity_b, relation, direction, co_occur, valid_from,
        valid_to}], "hops", "truncated"}`. `found=False, truncated=False`
        means not connected within the budget; `truncated=True` means the
        budget or the per-hop trim may have hidden a path.
        """
        payload: dict[str, Any] = {
            "org": self._agent.org,
            "namespace": self._agent.namespace,
            "entity_a": entity_a,
            "entity_b": entity_b,
            "max_hops": max_hops,
            "top_k_per_hop": top_k_per_hop,
            "min_co_occur": min_co_occur,
            "labeled": labeled,
        }
        if as_of is not None:
            payload["as_of"] = as_of.isoformat()
        if relations:
            payload["relations"] = list(relations)
        return await self._agent.transport.post("/v1/facts.path", payload)
