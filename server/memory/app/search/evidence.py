"""Evidence-row synthesis, namespace-scope merge, chunk dedup, and the
similarity floor for memory.search."""

from __future__ import annotations

import logging
from typing import Any

from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.search.scoring import _score_explanation

log = logging.getLogger(__name__)

# A synthetic evidence row sits just above the pool's best row — `pool_max + ε`, a bounded
# pin — not at a fixed offset such as `pool_max + 1.0`, which would say "first" and nothing
# else and push every computed row off the pool's own rank_score scale. `evidence_lift` in
# score_components is the amount above the ceiling; `pool_ceiling` is the ceiling itself.
EVIDENCE_LIFT_EPS = 1e-6


def _pinned_rank(pool: list[Any]) -> tuple[float, float]:
    """(rank, pool_ceiling) for a row that must lead the pool: the ceiling is
    the best `rank_score` in the pool (0.0 for an empty pool) and the rank
    is one ε above it, so no sort or cut can displace the row and the value
    stays on the pool's scale."""
    ceiling = max((float(getattr(h, "rank_score", 0.0) or 0.0) for h in pool), default=0.0)
    return ceiling + EVIDENCE_LIFT_EPS, ceiling


def _derived_evidence_hit(
    agg: dict[str, Any],
    pool: list[Any],
    *,
    namespace: str,
) -> Any:
    """Synthetic first-result row mirroring `derived_aggregate`.

    Readers that consume only `results[]` (context.pack, agents, external
    harnesses) must see the computed scalar, so it's rendered as an
    evidence row scored one ε above the pool max where no rank_score sort or truncation can
    displace it. The structured `derived_aggregate` field is kept unchanged alongside.
    """

    def _num(v: Any) -> str:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return str(int(f)) if f == int(f) else f"{f:g}"

    def _day(v: Any) -> str:
        iso = getattr(v, "isoformat", None)
        s = iso() if callable(iso) else str(v or "")
        return s[:10]

    op = str(agg.get("op") or "")
    n = int(agg.get("count") or 0)
    unit = ""
    for item in agg.get("items") or []:
        if item.get("unit"):
            unit = str(item["unit"])
            break
    if op == "sum" and agg.get("sum") is not None:
        total = _num(agg["sum"])
        if unit.lower() in ("usd", "dollar", "dollars", "$"):
            headline = f"total = ${total}"
        else:
            headline = f"total = {total} {unit}".rstrip()
    elif op == "date_span" and agg.get("span_days") is not None:
        headline = (
            f"span = {int(round(float(agg['span_days'])))} days "
            f"(first {_day(agg.get('first_at'))}, last {_day(agg.get('last_at'))})"
        )
    else:
        headline = f"count = {n}"
    item_bits: list[str] = []
    for item in (agg.get("items") or [])[:8]:
        obj = str(item.get("object") or "").strip()
        detail_parts: list[str] = []
        if item.get("numeric_value") is not None:
            detail_parts.append(f"{_num(item['numeric_value'])} {item.get('unit') or ''}".strip())
        day = _day(item.get("occurred_at"))
        if day:
            detail_parts.append(day)
        detail = ", ".join(p for p in detail_parts if p)
        item_bits.append(f"{obj} ({detail})" if detail else obj)
    excerpt = f"Computed from {n} dated records: {headline}"
    if item_bits:
        excerpt += " (items: " + "; ".join(b for b in item_bits if b) + ")"
    if agg.get("category_broadened"):
        excerpt += " [category filter broadened]"
    rank, ceiling = _pinned_rank(pool)
    return _MemorySearchHit(
        memory_key="derived_aggregate",
        memory_type="semantic",
        content_excerpt=excerpt[:1200],
        distance=0.0,
        similarity=1.0,
        rank_score=rank,
        source="derived",
        namespace=namespace,
        score_components={
            "derived_aggregate": 1.0,
            "pool_ceiling": ceiling,
            "evidence_lift": rank - ceiling,
            "rank_score": rank,
        },
        explanation=(
            "derived aggregate computed in ClickHouse over per-occurrence "
            "records; mirrors the structured derived_aggregate output field"
        ),
    )


def _temporal_diff_evidence_hit(
    diff: dict[str, Any],
    pool: list[Any],
    *,
    namespace: str,
) -> Any:
    """Pinned first-result row carrying the deterministic date interval.

    Reader date arithmetic is unreliable, so when the query is a
    date-difference shape and both operand event dates resolved, the
    interval is computed in ClickHouse (``dateDiff('day', a, b)``) and
    injected as evidence the reader REPORTS rather than recomputes. Scored one ε above the
    pool max so no later sort or truncation can displace it — same pinned posture as the
    derived-aggregate row.
    """
    days = int(diff.get("interval_days") or 0)
    from_date = str(diff.get("from_date") or "")
    to_date = str(diff.get("to_date") or "")
    # The injected line states the integer interval AND both event dates so
    # the reader can convert to the unit the question used (weeks/months)
    # from the day count instead of subtracting dates itself.
    excerpt = (
        f"[computed] interval = {days} days, "
        f"from {from_date} to {to_date} "
        f"(= {days / 7.0:.1f} weeks, {days / 30.4375:.1f} months). "
        "Computed in ClickHouse from the two event dates; "
        "report this interval rather than recomputing from dates."
    )
    from_ev = bool(diff.get("from_event_dated", True))
    to_ev = bool(diff.get("to_event_dated", True))
    if not (from_ev and to_ev):
        # A mention date sits at or after its event, so the bound's direction is known.
        # Stated as a bound the reader can report ("at least N"), not as a caution that
        # reads like missing evidence.
        if not from_ev and to_ev:
            excerpt += (
                f" The start date is when it was first mentioned, so the true interval "
                f"is AT LEAST {days} days; answer with 'at least' or 'about'."
            )
        elif from_ev and not to_ev:
            excerpt += (
                f" The end date is when it was mentioned, so the true interval is "
                f"AT MOST {days} days; answer with 'at most' or 'about'."
            )
        else:
            excerpt += (
                " Both dates are when the facts were mentioned rather than when they "
                "happened; answer with 'about' this interval."
            )
    rank, ceiling = _pinned_rank(pool)
    return _MemorySearchHit(
        memory_key="temporal_diff",
        memory_type="semantic",
        content_excerpt=excerpt[:1200],
        distance=0.0,
        similarity=1.0,
        rank_score=rank,
        source="computed",
        namespace=namespace,
        score_components={
            "temporal_diff": 1.0,
            "pool_ceiling": ceiling,
            "evidence_lift": rank - ceiling,
            "rank_score": rank,
        },
        explanation=(
            "deterministic date interval computed in ClickHouse with "
            "dateDiff over the two resolved event dates; the reader should "
            "report it rather than perform fragile date arithmetic"
        ),
    )


def _merge_namespace_scope(
    explicit: str, granted: list[str], *, ordered: bool = False
) -> list[str]:
    """Resolve the namespace set for namespace_scope="all_mine".

    `ordered=True` (scoped token carrying `ns_roles`): `granted` is the token's
    authoritative, priority-ordered namespace list — return it verbatim. Do NOT
    front-insert the addressed namespace; the token's order is strict and must
    not be reshuffled by which namespace the request happened to address. An
    addressed namespace the list lacks (cut by the fan-out cap or the kind
    filter) is read last.

    `ordered=False` (unscoped/wildcard token): the addressed namespace is ALWAYS
    searched and pinned to the front — grants/JWT membership may only WIDEN the
    set, never redirect away from the named namespace.
    """
    out = [ns for ns in granted if ns]
    if not explicit or explicit in out:
        return out
    return [*out, explicit] if ordered else [explicit, *out]


def _decorate_hits(
    hits: list[Any],
    *,
    input: MemorySearchInput,
) -> None:
    for h in hits:
        if getattr(h, "explanation", ""):
            continue
        ns = getattr(h, "namespace", "") or input.namespace
        source = getattr(h, "source", "fact") or "fact"
        resource = getattr(h, "source_resource_id", "") or ""
        sim = float(getattr(h, "similarity", 1.0 - getattr(h, "distance", 1.0)) or 0.0)
        rank = float(getattr(h, "rank_score", 0.0) or 0.0)
        resource_part = f"; source_resource_id={resource}" if resource else ""
        h.explanation = (
            f"{source} hit from namespace {ns}; similarity={sim:.2f}; "
            f"rank_score={rank:.2f}{resource_part}; {_score_explanation(input)}"
        )


def _dedup_redundant_chunks(hits: list[Any]) -> list[Any]:
    """Drop a chunk whose passage is near-duplicate of a higher-ranked item.

    Greedy, rank-ordered, LLM- and embedding-free (token-set containment /
    Jaccard). Keeps the higher-ranked carrier; a chunk near-identical to an
    item kept above it is redundant read-side token cost.

    ONLY chunk-source hits are dropped, and only against near-identical
    text. Facts/records/anchors are NEVER dropped: the tokenizer is
    alpha-only (digits stripped), so "score 124" and "score 132" tokenize
    identically — fact-vs-fact dedup would collapse two distinct numeric
    values into one (the de-atomization trap).
    """
    from server.memory.domain.passage_trim import (
        _content_token_set,
        token_set_is_near_duplicate,
    )

    if len(hits) < 2:
        return hits
    # Pre-tokenize each kept excerpt once: the pool is O(n²) pairwise, so
    # re-tokenizing both sides per comparison dominates CPU on a big pool.
    kept_sets: list[set[str]] = []
    out: list[Any] = []
    for h in hits:
        excerpt = getattr(h, "content_excerpt", "") or ""
        source = getattr(h, "source", "") or ""
        cand_set = _content_token_set(excerpt) if excerpt else set()
        if source == "chunk" and excerpt:
            if any(token_set_is_near_duplicate(cand_set, k) for k in kept_sets):
                continue
        out.append(h)
        if excerpt:
            kept_sets.append(cand_set)
    return out


def _floor_exempt(h: Any) -> bool:
    """A row the recency pool re-rank scored or the backfill added: the read asked for the
    newest in scope, and the newest row's cosine to "what did the latest run find" is low by
    construction — the ordering is the answer, so the floor does not judge it. Likewise a
    row that names the query's identifier."""
    comps = getattr(h, "score_components", None) or {}
    if bool(comps.get("recency_backfill")) or float(comps.get("recency_pool") or 0.0) > 0.0:
        return True
    # A row that carries the query's identifier verbatim (a known compound, or the planner's
    # identifier lift) is not semantic noise however low its cosine — the floor is a
    # statement about cosine.
    return bool(comps.get("compound_match")) or float(comps.get("identifier_match") or 0.0) >= 1.0


def _apply_min_similarity_floor(
    hits: list[Any], floor: float, *, top_k: int = 0, mode: str = "readmit"
) -> list[Any]:
    """Order floor-clearing hits first; keep the caller's top_k ask filled.

    `mode` decides what the fill is made of: "readmit" appends the sub-floor rows with their
    bodies (tagged `below_floor`);
    "ids_only" appends them with an empty `content_excerpt`, so the caller
    learns a weak match exists without its text entering the agent's
    context; "drop" appends nothing and skips the small-pool exemption, so
    the floor is a bound rather than a label.

    `hits` carry cosine *distance* (similarity = 1.0 - distance). Floor of
    0 disables filtering.

    The floor separates confident rows from the noise tail (it drives the
    abstain / calibration signals) but must not delete context the caller
    asked for. With `top_k` set, sub-floor candidates (similarity > 0) fill
    the remainder in score order, appended after the confident rows and
    tagged `below_floor`. Without `top_k`, sub-floor rows are dropped.

    Small-pool exemption: when the pool couldn't fill `top_k` (a sparse
    namespace, not a noisy one), the floor is skipped and the pool keeps its
    order — the floor only shapes confidence. `hits` arrives truncated to
    top_k, so len(hits) < top_k iff the pool was smaller than the ask.
    """
    if floor <= 0 or not hits:
        return hits
    mode = mode if mode in ("readmit", "ids_only", "drop") else "readmit"
    if mode == "readmit" and top_k > 0 and len(hits) < top_k:
        log.info(
            "memory.search: pool (%d) smaller than top_k (%d); "
            "similarity floor %.3f skipped — calibration marks confidence",
            len(hits),
            top_k,
            floor,
        )
        return hits
    # A row the recency re-rank ordered or the backfill added answers "the newest in scope";
    # the floor does not judge it.
    kept = [h for h in hits if (1.0 - h.distance) >= floor or _floor_exempt(h)]
    dropped = len(hits) - len(kept)
    if top_k <= 0 or dropped == 0:
        if dropped > 0:
            log.info(
                "memory.search: dropped %d/%d hit(s) below similarity floor %.3f",
                dropped,
                len(hits),
                floor,
            )
        return kept
    if mode == "drop":
        log.info(
            "memory.search: dropped %d/%d hit(s) below similarity floor %.3f (floor_mode=drop)",
            dropped,
            len(hits),
            floor,
        )
        return kept
    fill_budget = max(0, top_k - len(kept))
    if fill_budget == 0:
        return kept
    # `hits` is already in score order. Zero/negative-similarity rows are
    # pure noise and never filled.
    fillers = [h for h in hits if 0.0 < (1.0 - h.distance) < floor][:fill_budget]
    for h in fillers:
        comps = getattr(h, "score_components", None)
        if isinstance(comps, dict):
            comps["below_floor"] = 1.0
        if mode == "ids_only":
            try:
                h.content_excerpt = ""
            except Exception:  # noqa: BLE001 — a frozen test double stays as is
                pass
    log.info(
        "memory.search: %d/%d hit(s) below similarity floor %.3f kept as "
        "fill for requested top_k=%d",
        len(fillers),
        len(hits),
        floor,
        top_k,
    )
    return kept + fillers
