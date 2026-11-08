"""Deterministic query planning for memory.search.

The search handler owns execution. This service turns the enrichment
signals into an explicit, inspectable plan so ranking/cost choices live in
one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.memory.domain.search_types import RANK_SCORE_PRE_RERANK

# Identifier-shaped tokens: a UUID, a hash-like hex run, a
# token that mixes letters and digits (a build id, a handle with digits, a
# key), and the PR / issue / @handle forms the calibrator already extracts.
# A plain word or a bare year is not one. These are the queries an embedder
# cannot place: two UUIDs are as similar as any two, so the lexical leg is
# the only one that can find the row.
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX_RE = re.compile(r"\b[0-9a-f]{12,64}\b", re.I)
# Read from the first word boundary of each `[A-Za-z0-9_-]` run, the one
# place a match can start: the lookaheads then scan a run once, not once per
# boundary in it.
_MIXED_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?>[A-Za-z0-9_-]*?\b)"
    r"((?=[A-Za-z0-9_-]{6,}\b)(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]+\b)"
)
_ORDINAL_WORD_RE = re.compile(r"^\d+(st|nd|rd|th)$", re.I)


def identifier_tokens(query_text: str) -> list[str]:
    """The identifier-shaped tokens a query carries, first-seen order,
    case-insensitively deduplicated. Empty for ordinary prose."""
    text = query_text or ""
    if not text.strip():
        return []
    seen: set[str] = set()
    out: list[str] = []

    def _add(tok: str) -> None:
        t = tok.strip().lstrip("@")
        if not t or _ORDINAL_WORD_RE.match(t) or t.lower() in seen:
            return
        seen.add(t.lower())
        out.append(t)

    for m in _UUID_RE.finditer(text):
        _add(m.group(0))
    # A UUID's own segments are hex runs; mask the UUIDs before the other passes.
    text = _UUID_RE.sub(" ", text)
    for m in _HEX_RE.finditer(text):
        _add(m.group(0))
    for m in _MIXED_RE.finditer(text):
        tok = m.group(1)
        # A date-like or version-like run ("2026-09-12", "v2") is not an id.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok) or re.fullmatch(r"v\d+(\.\d+)*", tok, re.I):
            continue
        _add(tok)
    try:
        from server.memory.domain.retrieval_calibration import _salient_identifiers

        for ident in _salient_identifiers(text):
            _add(ident)
    except Exception:  # noqa: BLE001 — the calibrator is optional here
        pass
    return out[:8]


@dataclass(frozen=True)
class MemorySearchPlan:
    search_mode: str
    hyde: bool
    include_top_n_unmatched: int
    rerank: str
    planes: list[str] = field(default_factory=list)
    rewrite_budget: int = 0
    reason: str = ""
    budget_class: str = "standard"
    estimated_work: dict[str, Any] = field(default_factory=dict)
    # The knobs the namespace's retrieval_defaults filled.
    namespace_defaults: tuple[str, ...] = ()

    @property
    def input_updates(self) -> dict[str, Any]:
        updates: dict[str, Any] = {
            "search_mode": self.search_mode,
            "hyde": self.hyde,
            "include_top_n_unmatched": self.include_top_n_unmatched,
        }
        if self.rerank:
            updates["rerank"] = self.rerank
        return updates

    def explain(self) -> str:
        planes = ",".join(self.planes) if self.planes else "fact"
        bits = [
            f"mode={self.search_mode}",
            f"planes={planes}",
            f"hyde={'on' if self.hyde else 'off'}",
        ]
        if self.include_top_n_unmatched:
            bits.append(f"pool+={self.include_top_n_unmatched}")
        if self.rerank:
            bits.append("rerank=on")
        if self.rewrite_budget:
            bits.append(f"rewrite_budget={self.rewrite_budget}")
        if self.budget_class:
            bits.append(f"budget={self.budget_class}")
        if self.estimated_work:
            est = self.estimated_work
            bits.append(
                "est="
                f"llm:{int(est.get('llm_calls', 0))},"
                f"emb:{int(est.get('embedding_calls', 0))},"
                f"vec:{int(est.get('vector_queries', 0))},"
                f"cand:{int(est.get('candidate_pool', 0))}"
            )
        if self.reason:
            bits.append(f"reason={self.reason}")
        if self.namespace_defaults:
            bits.append(f"ns_defaults={','.join(self.namespace_defaults)}")
        return ", ".join(bits)


def _planes_with_unmatched(
    planes: list[str],
    include_top_n_unmatched: int,
) -> list[str]:
    """Keep candidate-pool widening visible in the executable route mask."""

    out = list(dict.fromkeys(planes))
    if include_top_n_unmatched > 0 and "unmatched" not in out:
        out.append("unmatched")
    return out


def plan_memory_search(
    *,
    current_search_mode: str,
    current_hyde: bool,
    current_include_top_n_unmatched: int,
    current_rerank: str,
    recipe: str = "",
    query_shape: str = "",
    resolved_dates: list[str] | None = None,
    preference_attribute: str = "",
    entity_count: int = 0,
    sub_query_count: int = 0,
    recency_intent: bool = False,
    chronological_intent: str = "",
    budget_class: str = "standard",
    query_text: str = "",
    mode_pinned: bool = True,
    namespace_defaults: tuple[str, ...] = (),
) -> MemorySearchPlan:
    """Return the execution plan for a memory.search request.

    Explicit recipes remain authoritative. Otherwise the planner only
    changes the default posture (`search_mode='rrf'`) when enrichment gives
    a strong shape signal — and, whatever the deployment's default mode, an
    identifier-shaped query on an unpinned mode takes the lexical fusion:
    `mode_pinned` is False only when the caller set neither
    `search_mode` nor a recipe and the handler filled the mode from
    `memory_search_default_mode` — or from the namespace's own
    `retrieval_defaults`, whose applied knobs
    `namespace_defaults` names so the plan's explain string carries them.
    """

    resolved_dates = resolved_dates or []
    search_mode = current_search_mode
    budget_class = budget_class if budget_class in {"fast", "standard", "deep"} else "standard"
    hyde = bool(current_hyde)
    include_pool = int(current_include_top_n_unmatched or 0)
    rerank = current_rerank
    planes = ["fact", "chunk", "lexical"] if current_search_mode == "rrf" else ["fact", "chunk"]
    rewrite_budget = 0
    reason = "caller"

    if recipe:
        reason = f"recipe:{recipe}"
        if recipe == "fast":
            planes = ["fact"]
        elif recipe == "balanced":
            planes = ["fact", "chunk", "lexical"]
        elif recipe == "graph":
            planes = ["fact", "graph", "chunk"]
        elif recipe == "temporal":
            planes = ["record", "fact", "temporal", "chunk"]
        elif recipe == "quality":
            planes = ["fact", "chunk", "lexical"]
            include_pool = max(include_pool, 50)
        elif recipe in {"verbatim", "code"}:
            planes = ["fact", "chunk", "lexical"]
        return MemorySearchPlan(
            search_mode=search_mode,
            hyde=hyde,
            include_top_n_unmatched=include_pool,
            rerank=rerank,
            planes=_planes_with_unmatched(planes, include_pool),
            rewrite_budget=rewrite_budget,
            reason=reason,
            budget_class=budget_class,
            namespace_defaults=tuple(namespace_defaults),
        )

    # Only reinterpret the default. If the caller explicitly chose lexical,
    # graph, temporal, etc., preserve that choice and just annotate planes.
    identifier_hit = (
        not recipe
        and not mode_pinned
        and current_search_mode not in {"lexical", "bm25_hybrid"}
        and bool(identifier_tokens(query_text))
    )
    if identifier_hit:
        # A UUID, hash or build id has no neighbourhood in
        # embedding space — the semantic default ranked a wrong UUID above the
        # right one at 0.705 vs 0.655 — but it is one exact token to the
        # lexical index. Fuse the lexical leg in; keep the caller's k.
        search_mode = "bm25_hybrid"
        planes = ["fact", "lexical", "chunk"]
        include_pool = max(include_pool, 30)
        reason = "identifier"
    elif current_search_mode == "rrf":
        if query_shape == "needle":
            search_mode = "bm25_hybrid"
            hyde = hyde or budget_class == "deep"
            include_pool = max(include_pool, 50)
            planes = ["record", "fact", "chunk", "lexical"]
            rewrite_budget = 1
            reason = "needle"
        elif preference_attribute:
            search_mode = "bm25_hybrid"
            planes = ["record", "fact", "chunk", "lexical"]
            reason = "preference_attribute"
        elif query_shape in {"aggregational", "temporal_diff"}:
            search_mode = "bm25_hybrid"
            hyde = hyde or budget_class == "deep"
            include_pool = max(include_pool, 100)
            planes = ["record", "fact", "chunk", "lexical", "subquery"]
            rewrite_budget = 1
            reason = query_shape
        elif resolved_dates and query_shape == "single_fact":
            search_mode = "temporal"
            planes = ["record", "fact", "temporal", "chunk"]
            reason = "date_anchor"
        elif entity_count >= 2 or sub_query_count > 0:
            search_mode = "graph"
            planes = ["fact", "graph", "chunk"]
            include_pool = max(include_pool, 30)
            reason = "entity_graph"
        else:
            reason = "default_rrf"
    elif current_search_mode == "graph":
        planes = ["fact", "graph", "chunk"]
        reason = "caller_graph"
    elif current_search_mode == "temporal":
        planes = ["record", "fact", "temporal", "chunk"]
        reason = "caller_temporal"
    elif current_search_mode in {"lexical", "bm25_hybrid"}:
        planes = ["fact", "lexical", "chunk"]
        reason = f"caller_{current_search_mode}"

    return MemorySearchPlan(
        search_mode=search_mode,
        hyde=hyde,
        include_top_n_unmatched=include_pool,
        rerank=rerank,
        planes=_planes_with_unmatched(planes, include_pool),
        rewrite_budget=rewrite_budget,
        reason=reason,
        budget_class=budget_class,
        namespace_defaults=tuple(namespace_defaults),
    )


def estimate_memory_search_work(
    plan: MemorySearchPlan,
    *,
    namespace_count: int,
    top_k: int,
    pool_top_k: int,
    sub_query_count: int = 0,
    entity_count: int = 0,
    graph_triples_enabled: bool = False,
    external_rerank_enabled: bool = False,
    llm_rerank: bool = False,
) -> dict[str, Any]:
    """Estimate retrieval work before executing the expensive legs.

    The estimate is intentionally simple and deterministic. It is not a
    billing oracle; it gives the handler and operators a stable shape-level
    view of why an agentic query is expensive.
    """

    ns = max(1, int(namespace_count or 1))
    sub_queries = max(0, int(sub_query_count or 0))
    entities = max(0, int(entity_count or 0))
    planes = set(plan.planes or [])

    vector_queries = ns
    if sub_queries and "subquery" in planes:
        vector_queries += ns * sub_queries
    if entities:
        vector_queries += entities  # entity_vectors ANN lookups

    llm_calls = 0
    if plan.hyde:
        llm_calls += 1
    if graph_triples_enabled:
        llm_calls += 1
    if llm_rerank:  # the builtin:openai reranker; a rerank service is no LLM call
        llm_calls += 1

    candidate_pool = ns * int(pool_top_k)
    if sub_queries and "subquery" in planes:
        candidate_pool += ns * sub_queries * max(5, min(int(top_k), int(pool_top_k)))
    if plan.include_top_n_unmatched and "unmatched" in planes:
        candidate_pool += ns * int(plan.include_top_n_unmatched)

    return {
        "budget_class": plan.budget_class,
        "llm_calls": int(llm_calls),
        "embedding_calls": int(1 + (sub_queries if "subquery" in planes else 0) + entities),
        "vector_queries": int(vector_queries),
        "lexical_queries": int(ns if planes.intersection({"lexical", "chunk"}) else 0),
        "graph_queries": int(ns if "graph" in planes else 0),
        "structured_queries": int(ns if "record" in planes else 0),
        "namespace_count": int(ns),
        "top_k": int(top_k),
        "pool_top_k": int(pool_top_k),
        "candidate_pool": int(candidate_pool),
        "sub_query_count": int(sub_queries),
        "entity_count": int(entities),
        "external_rerank_enabled": bool(external_rerank_enabled),
    }


def bound_component_lift(
    hits: list[Any],
    *,
    component: str = "chunk_parent_boost",
    lift_key: str = "chunk_parent_lift",
    eps: float = 1e-6,
) -> int:
    """Rows lifted by the additive `component` (the chunk parent boost, up to
    +0.40 on a cosine) that sit above every un-lifted row are re-placed at
    `ceiling + k·ε` in their own order — the same shape as the identifier pin —
    so `rank_score` stays on the pool's scale (otherwise the boost alone can
    push a row to ≥ 1.0). Same set, same order: a lifted row above the
    ceiling stays above it, lifted rows keep their order among themselves, a
    lifted row at or below the ceiling and every un-lifted row are untouched,
    and no row is ever raised. `lift_key` in score_components is the
    effective lift over the row's un-boosted score; the nominal `component`
    stays as the leg wrote it. `hits` must be sorted best-first. Returns how
    many rows moved; 0 when nothing is lifted or nothing is un-lifted."""
    lifted: list[Any] = []
    ceiling: float | None = None
    for h in hits:
        comps = getattr(h, "score_components", None) or {}
        if float(comps.get(component, 0.0) or 0.0) > 0.0:
            lifted.append(h)
        else:
            own = float(getattr(h, "rank_score", 0.0) or 0.0)
            ceiling = own if ceiling is None else max(ceiling, own)
    if not lifted or ceiling is None:
        return 0
    above = [h for h in lifted if float(getattr(h, "rank_score", 0.0) or 0.0) > ceiling]
    n = len(above)
    moved = 0
    for i, h in enumerate(above):
        own = float(getattr(h, "rank_score", 0.0) or 0.0)
        target = ceiling + (n - i) * eps
        if target >= own:
            continue  # already within ε of the ceiling; never raise a row
        comps = h.score_components
        boost = float(comps.get(component, 0.0) or 0.0)
        h.rank_score = target
        # A reranked row holds another rank's score; its own is kept beside it.
        unboosted = float(comps.get(RANK_SCORE_PRE_RERANK, own)) - boost
        comps[lift_key] = target - unboosted
        comps["rank_score"] = target
        moved += 1
    return moved


def pin_identifier_hits(hits: list[Any], identifiers: list[str]) -> int:
    """A hit whose text carries one of the query's identifier tokens outranks
    every hit that does not.

    Fusion is a vote among legs, and the semantic leg votes for the
    identifier's nearest neighbours — under the default posture the exact pair
    can fuse to rank four of five, past a chained lookup's `top_k=3`. An
    exact identifier is not one signal among several; it is the answer to
    the question the identifier asks. Matching hits keep their relative
    order and are lifted above the top non-matching score; the component
    `identifier_match` marks them. Returns how many were lifted. Pure: sorts
    `hits` in place, no I/O.

    The lift is bounded: a matching row lands just above the
    best non-matching row — `max(own, ceiling) + ε` — never `ceiling + 1.0`.
    A constant lift would put many top-1 `rank_score`s above 1.0 in a
    namespace whose tokenised paths are identifier-shaped, so a score that
    read as a cosine everywhere else read as 1.6 here. The amount lifted
    rides in `identifier_lift`; a row already above every non-match keeps
    its own score (lift 0)."""
    idents = [t.lower() for t in (identifiers or []) if t]
    if not idents or not hits:
        return 0
    matched: list[Any] = []
    rest: list[Any] = []
    for h in hits:
        text = str(getattr(h, "content_excerpt", "") or "").lower()
        key = str(getattr(h, "memory_key", "") or "").lower()
        (matched if any(t in text or t in key for t in idents) else rest).append(h)
    if not matched:
        return 0
    if not rest:
        return len(matched)  # every hit matches: nothing to lift over
    ceiling = max(float(getattr(h, "rank_score", 0.0) or 0.0) for h in rest)
    n = len(matched)
    for i, h in enumerate(matched):
        own = float(getattr(h, "rank_score", 0.0) or 0.0)
        # Just above every non-match, order kept, scale kept: the pool's own
        # ceiling, not ceiling + 1.0.
        lifted = max(own, ceiling) + (n - i) * 1e-6
        h.rank_score = lifted
        try:
            h.score_components["identifier_match"] = 1.0
            h.score_components["identifier_lift"] = lifted - own
            h.score_components["rank_score"] = lifted
        except Exception:  # noqa: BLE001 — a hit without components still moves
            pass
    hits[:] = matched + rest
    return n
