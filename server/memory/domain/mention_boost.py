"""Pool-consensus mention boost.

Retrieved nodes are reranked by how many of the retrieved episodes mention them: salience
within the answer set, not in the corpus. The read is cheap: one point read of `entities`
for the pool's keys (the legs do not select the column), then arithmetic. After global
fusion, count how many entity-bearing hits in the pool mention each entity; a hit whose
entities are mentioned by many other candidates is the pool agreeing on what the question is
about, and it moves up by a bounded amount.

Two guards keep it from rewarding the obvious: an entity mentioned by more
than `ubiquity` of the entity-bearing pool (the speakers' names in a
two-person chat) carries no signal and is ignored; and the boost is capped
by the request's prior_cap so it can never invert a cosine
band. Default weight 0: the pool is untouched unless a request or the
deployment asks.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from server.memory.domain.search_types import hit_sort_key


def hit_entities(h: Any, entities_by_key: dict[str, list[str]] | None = None) -> set[str]:
    """Lower-cased entity set of a hit: its own `entities` when the hit type
    carries them, else the store's list for its key (the pool's pydantic
    hits carry no entities; the handler point-reads them once)."""
    ents = getattr(h, "entities", None)
    if not ents and entities_by_key:
        ents = entities_by_key.get(str(getattr(h, "memory_key", "") or ""))
    return {str(e).strip().lower() for e in (ents or []) if e}


def entity_consensus(
    hits: list[Any],
    *,
    ubiquity: float = 0.5,
    entities_by_key: dict[str, list[str]] | None = None,
) -> tuple[Counter[str], int]:
    """(entity -> number of entity-bearing hits mentioning it, number of
    entity-bearing hits). Entities present in more than ``ubiquity`` of the
    entity-bearing hits are dropped from the counter."""
    counts: Counter[str] = Counter()
    bearing = 0
    for h in hits:
        ents = hit_entities(h, entities_by_key)
        if not ents:
            continue
        bearing += 1
        counts.update(ents)
    if bearing:
        limit = max(1.0, float(ubiquity) * bearing)
        for e in [e for e, c in counts.items() if c > limit]:
            del counts[e]
    return counts, bearing


def apply_mention_boost(
    hits: list[Any],
    *,
    weight: float,
    cap: float,
    ubiquity: float = 0.5,
    entities_by_key: dict[str, list[str]] | None = None,
) -> dict[str, float | int]:
    """Add ``min(cap, weight · s)`` to each hit's rank_score, where ``s`` in
    [0, 1] is the hit's mean over its entities of log1p(count − 1) /
    log1p(max − 1) — an entity mentioned by one hit only scores 0. Re-sorts
    by the shared sort key. Returns work counters; no-op when weight ≤ 0 or
    the pool has no consensus."""
    weight = float(weight or 0.0)
    if weight <= 0.0 or not hits:
        return {"mention_boost_applied": 0, "mention_boost_max": 0.0}
    counts, bearing = entity_consensus(hits, ubiquity=ubiquity, entities_by_key=entities_by_key)
    top = max(counts.values(), default=0)
    if bearing < 2 or top < 2:
        return {"mention_boost_applied": 0, "mention_boost_max": 0.0}
    denom = math.log1p(top - 1)
    applied = 0
    max_boost = 0.0
    for h in hits:
        ents = hit_entities(h, entities_by_key)
        scores = [math.log1p(counts[e] - 1) / denom for e in ents if counts.get(e, 0) >= 2]
        if not scores:
            continue
        s = sum(scores) / len(ents)  # ubiquitous / singleton entities dilute, by design
        boost = min(float(cap), weight * s)
        if boost <= 0.0:
            continue
        h.rank_score = float(getattr(h, "rank_score", 0.0) or 0.0) + boost
        comps = getattr(h, "score_components", None)
        if isinstance(comps, dict):
            comps["mention_boost"] = round(boost, 5)
        applied += 1
        max_boost = max(max_boost, boost)
    if applied:
        hits.sort(key=hit_sort_key)
    return {"mention_boost_applied": applied, "mention_boost_max": round(max_boost, 5)}


__all__ = ["apply_mention_boost", "entity_consensus", "hit_entities"]
