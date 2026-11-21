"""Rank admitted chunks without combining distinct source copies of a public address."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from server.knowledge.app.search import ChunkHit, _provenance, _structure, fuse_rrf
from server.knowledge.app.source_access import Admitted
from server.knowledge.app.source_search import Hydrated
from server.knowledge.domain.document_access import public_tags
from server.knowledge.store.source_candidates import Leg


@dataclass(frozen=True)
class RankedHit:
    chunk: ChunkHit
    admitted: Admitted | None = None

    @property
    def mirrored(self) -> bool:
        return self.admitted is not None


def ordinary_families(pools: dict[Leg, list[ChunkHit]]) -> set[tuple[str, str]]:
    """Only supplied serving ordinary pools can shadow a checked source copy."""
    return {(hit.namespace, hit.source_uri) for pool in pools.values() for hit in pool}


def _key(hit: ChunkHit) -> tuple[str, str, int, int, int]:
    return hit.namespace, hit.source_uri, hit.chunk_idx, hit.doc_version, hit.revision


def _mirror_hit(item: Hydrated, leg: Leg) -> ChunkHit:
    candidate, row = item.admitted.candidate, item.row
    distance, lexical = float(candidate["distance"]), float(candidate["lexical"])
    return ChunkHit(
        namespace=candidate["namespace"],
        source_uri=candidate["source_uri"],
        chunk_idx=int(candidate["chunk_idx"]),
        doc_version=int(candidate["doc_version"]),
        content=row["content"],
        context=row.get("context", ""),
        tags=list(row.get("tags") or []),
        distance=distance,
        lexical_score=lexical,
        score=lexical if leg == "lexical" else 1.0 - distance,
        keys_matched=tuple(candidate["matched"]),
        ingested_at=row.get("ingested_at"),
        **_structure(row.get("chunk_metadata")),
        **_provenance(row),
    )


def _order(hit: ChunkHit, leg: Leg) -> tuple[Any, ...]:
    key = (hit.distance, *_key(hit))
    if leg == "lexical":
        return (-hit.lexical_score, *key)
    if leg == "keys":
        return (-len(hit.keys_matched), *key)
    return key


def rank(
    ordinary: dict[Leg, list[ChunkHit]],
    mirrored: dict[Leg, list[Hydrated]],
    *,
    legs: tuple[Leg, ...],
    fallback: list[ChunkHit],
    top_k: int,
    lexical_weight: float = 1.0,
) -> list[RankedHit]:
    """Fuse storage identities, then select complete copies before the public limit."""
    shadows = ordinary_families({leg: ordinary.get(leg, []) for leg in legs})
    pools = {leg: list(ordinary.get(leg, [])) for leg in legs}
    proofs = {}
    for leg in legs:
        for item in mirrored.get(leg, []):
            admitted = item.admitted
            family = (admitted.namespace, admitted.candidate["source_uri"].rsplit("#", 1)[0])
            if family in shadows:
                continue
            hit = _mirror_hit(item, leg)
            proofs[_key(hit)] = admitted
            pools[leg].append(hit)
    if not proofs:
        return [RankedHit(hit) for hit in fallback]
    pools = {leg: sorted(pool, key=lambda hit: _order(hit, leg)) for leg, pool in pools.items()}
    if len(legs) == 1:
        ranked = pools[legs[0]]
    else:
        # An early fused limit could be consumed by different copies of one chunk.
        ranked = fuse_rrf(
            pools.get("semantic", []),
            pools.get("lexical", []),
            keyed=pools.get("keys", []),
            top_k=sum(map(len, pools.values())),
            w_lex=lexical_weight,
            by_version=True,
        )
    out, seen = [], set()
    for hit in ranked:
        proof = proofs.get(_key(hit))
        if proof is not None:
            hit = replace(
                hit,
                namespace=proof.namespace,
                source_uri=hit.source_uri.rsplit("#", 1)[0],
                tags=public_tags(hit.tags),
            )
        key = (hit.namespace, hit.source_uri, hit.chunk_idx)
        if key not in seen:
            seen.add(key)
            out.append(RankedHit(hit, proof))
            if len(out) >= top_k:
                break
    return out
