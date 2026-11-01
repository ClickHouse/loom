"""Scoring, rerank ordering, and score-explanation helpers for memory.search."""

from __future__ import annotations

from typing import Any

from server.memory.api.models_search import MemorySearchInput
from server.memory.domain.search_types import RANK_SCORE_PRE_RERANK
from server.settings import settings


def _effective_ann_boosts(hits: list[Any], *, rrf_pool: bool) -> dict[int, float]:
    """Per-hit effective entity-ANN boost for global fusion.

    Invariant: no single boost may exceed the pool's max raw base score.
    The raw ANN boost is on a fixed [0, 0.5] scale while base scores are
    mode-dependent (rrf ~[0, 0.033]; additive modes ~[0, 2]), so naive
    addition lets one entity match outweigh an entire rrf ranking.

    - rrf pools: the boost joins as a third RRF arm — ranked by strength,
      contributing 1/(60 + rank), the same scale as the other arms.
    - additive pools: the raw boost is kept but clamped to the pool's max
      raw base.

    Double-count guard: rows whose SQL rank_score already includes the
    exact-intersection entity boost get the ANN boost halved.

    Returns {hit_index: effective_boost}; absent indices = no boost.
    """
    raws = [
        float((getattr(h, "score_components", None) or {}).get("rank_score_raw", h.rank_score))
        for h in hits
    ]
    pool_max = max(raws) if raws else 0.0
    adjusted: dict[int, float] = {}
    for i, h in enumerate(hits):
        comps = getattr(h, "score_components", None) or {}
        ann = float(comps.get("entity_ann_boost", 0.0) or 0.0)
        if ann <= 0.0:
            continue
        if float(comps.get("entity_boost_sql", 0.0) or 0.0) > 0.0:
            ann *= 0.5
        adjusted[i] = ann
    if not adjusted:
        return {}
    if rrf_pool:
        ranked = sorted(adjusted, key=lambda i: -adjusted[i])
        return {i: 1.0 / (60.0 + r) for r, i in enumerate(ranked, start=1)}
    cap = max(pool_max, 0.0)
    return {i: min(b, cap) for i, b in adjusted.items()}


# Rows the system wrote about itself carry these key shapes and are `asserted` by the
# crystalliser; a person's fact is not one of them.
_SYSTEM_KEY_PREFIXES = ("hint:", "procedure", "summary:", "obs:", "chunk:", "reflection:")


def asserted_component(hits: list[Any]) -> int:
    """Stamp `score_components["asserted"] = 1.0` on every fact hit a person
    asserted by hand (source_class `asserted`, not a system-shaped key, not
    procedural), so `_effective_arm_boosts` can give it the tie-break the
    key-match arm gets. Returns how many were stamped."""
    n = 0
    for h in hits:
        if (getattr(h, "source", "") or "fact") != "fact":
            continue
        # A fact hit whose score came from one of its memory.set chunks
        # (`fold_set_chunks`, `folded_lift`) ranks where the chunk ranked; the
        # chunk never had this tie-break, so the folded row does not gain it. With the boost,
        # every folded parent would step +0.03 past the relied-on facts at ranks 6–9.
        if float((getattr(h, "score_components", None) or {}).get("folded_lift", 0.0)) > 0.0:
            continue
        if str(getattr(h, "source_class", "") or "") != "asserted":
            continue
        key = str(getattr(h, "memory_key", "") or "")
        if key.startswith(_SYSTEM_KEY_PREFIXES):
            continue
        if str(getattr(h, "memory_type", "") or "") == "procedural":
            continue
        comps = getattr(h, "score_components", None)
        if comps is None:
            comps = {}
            try:
                h.score_components = comps
            except Exception:  # noqa: BLE001 — a frozen hit keeps its score
                continue
        comps["asserted"] = 1.0
        n += 1
    return n


def _effective_arm_boosts(
    hits: list[Any], *, component: str, rrf_pool: bool, scale: float = 0.5
) -> dict[int, float]:
    """Per-hit effective boost for one additive arm carried as a [0, 1]
    `score_components[component]` strength (the key-match arm).
    Same invariant as the entity-ANN boost: in rrf pools the arm joins as
    one more RRF arm ranked by strength (1/(60 + rank)); in additive pools
    the strength is scaled to `scale` and clamped to the pool's max raw base,
    so a key that matches perfectly can lift a row to the head of the pool
    and never past a stronger relevance score."""
    strengths: dict[int, float] = {}
    raws: list[float] = []
    for i, h in enumerate(hits):
        comps = getattr(h, "score_components", None) or {}
        raws.append(float(comps.get("rank_score_raw", h.rank_score)))
        v = float(comps.get(component, 0.0) or 0.0)
        if v > 0.0:
            strengths[i] = min(1.0, v)
    if not strengths:
        return {}
    if rrf_pool:
        ranked = sorted(strengths, key=lambda i: -strengths[i])
        return {i: 1.0 / (60.0 + r) for r, i in enumerate(ranked, start=1)}
    cap = max(max(raws) if raws else 0.0, 0.0)
    return {i: min(v * scale, cap) for i, v in strengths.items()}


def _anchor_rank_score(similarity: float, pool_floor: float) -> float:
    """Entry score for an anchor-arm row: strictly below the weakest
    main-pool candidate, ordered among anchors by similarity.

    Anchor rows are recall candidates pulled in by hard entity/date
    predicates, not ranked evidence — they fill the tail unless a later
    boost or the rerank promotes them. `pool_floor` is the pool's
    minimum raw base score (0.0 for an empty pool).
    """
    return pool_floor - 1e-6 - (1.0 - float(similarity))


def drop_rerank_trail(hit: Any) -> None:
    """Take a rerank's marks off a hit that joins another pool: every `rerank_*` entry
    `_record_rerank_trail` stamps and the pre-rerank score. The row keeps the score its own
    pool served it (after a rerank, that pool's handed-out score)."""
    comps = hit.score_components
    for key in [k for k in comps if k.startswith("rerank_") or k == RANK_SCORE_PRE_RERANK]:
        del comps[key]


def _record_rerank_trail(before: list[Any], after: list[Any], source: str) -> dict[str, float]:
    """Stamp what a reranker moved. Each hit gets
    `rerank_from` (its position before), `rerank_pos` (after), `rerank_delta`
    (from − pos: positive = promoted) and `rerank_<source>` = 1.0 in
    score_components; returns the pool-level summary the handler puts on
    retrieval_work — moved count and the largest move. A reranker is the one
    stage that re-orders every hit with no term in the SQL; this is the trail
    an operator reads to ask what it did and to whom."""
    pos_before = {id(h): i for i, h in enumerate(before)}
    moved = 0
    max_move = 0
    for i, h in enumerate(after):
        frm = pos_before.get(id(h), i)
        delta = frm - i
        if delta != 0:
            moved += 1
            max_move = max(max_move, abs(delta))
        h.score_components = {
            **(getattr(h, "score_components", None) or {}),
            "rerank_from": float(frm),
            "rerank_pos": float(i),
            "rerank_delta": float(delta),
            f"rerank_{source}": 1.0,
        }
    return {"rerank_moved": float(moved), "rerank_max_move": float(max_move)}


def _score_explanation(input: MemorySearchInput) -> str:
    bits = [f"mode={input.search_mode}"]
    if input.recipe:
        bits.append(f"recipe={input.recipe}")
    if input.hyde:
        bits.append("hyde=on")
    if input.rerank:
        bits.append("rerank=requested")
    if input.tags:
        bits.append("tags=filtered")
    return ", ".join(bits)


def rows_per_key(query_shape: str) -> int:
    """How many rows of one key the final block keeps; 0 = every one. Extraction writes a
    key once per session, so one attribute could fill the window; an aggregational or
    temporal-difference query wants every row of the key."""
    if query_shape in ("aggregational", "temporal_diff"):
        return 0
    return int(settings.memory_search_per_key_cap)
