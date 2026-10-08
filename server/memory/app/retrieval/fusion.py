"""Ordering the fused fact pool: the composite rank sort for the
cosine-ordered modes, and reciprocal-rank fusion of the lexical and semantic
orders for rrf, with the lexical weight adapted to how code-shaped the query
is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from server.memory.domain.search_types import SearchHit, SearchQuery, hit_sort_key

_CODE_EXTENSIONS = frozenset(
    {
        "cpp",
        "h",
        "hpp",
        "cc",
        "cxx",
        "c",
        "sql",
        "py",
        "sh",
        "xml",
        "txt",
        "cmake",
        "md",
        "yaml",
        "yml",
        "json",
        "rs",
        "go",
        "js",
        "ts",
        "java",
        "html",
        "proto",
        "toml",
        "ini",
        "cfg",
    }
)


def rrf_scores(hits: Sequence[Any], *, w_lex: float, k: float = 60.0) -> list[float]:
    """Reciprocal-rank fusion of the lexical and semantic orders of `hits`,
    one score per input position. Both rank orders are total: the lexical
    order breaks ties by distance then key,
    the semantic order by lexical score then key, so the scores — and the
    window cut from them — are a function of the hits, not of the order the
    legs returned them in."""
    by_lex = sorted(
        range(len(hits)),
        key=lambda i: (
            -float(getattr(hits[i], "rank_score", 0.0) or 0.0),
            float(getattr(hits[i], "distance", 0.0) or 0.0),
            str(getattr(hits[i], "memory_key", "") or ""),
        ),
    )
    by_sem = sorted(
        range(len(hits)),
        key=lambda i: (
            float(getattr(hits[i], "distance", 0.0) or 0.0),
            -float(getattr(hits[i], "rank_score", 0.0) or 0.0),
            str(getattr(hits[i], "memory_key", "") or ""),
        ),
    )
    lex_rank = {i: r + 1 for r, i in enumerate(by_lex)}
    sem_rank = {i: r + 1 for r, i in enumerate(by_sem)}
    return [1.0 / (k + sem_rank[i]) + w_lex / (k + lex_rank[i]) for i in range(len(hits))]


def _rrf_code_shape(query_text: str) -> float:
    """Fraction of query tokens that look like code/path fragments rather
    than prose: CamelCase identifiers (`MergeTreeIndexBloomFilter`), file
    extensions (`cpp`, `sql`), path separators, or bare digit runs.

    High for the tokenized-file-path queries a code-review agent sends;
    ~0 for a natural-language question. Pure function of the query text
    (no corpus round-trip), so it's cheap and unit-testable.
    """
    tokens = query_text.split()
    if not tokens:
        return 0.0
    codey = 0
    for t in tokens:
        core = "".join(c for c in t if c.isalnum())
        is_camel = (
            any(t[i].islower() and t[i + 1].isupper() for i in range(len(t) - 1))
            or sum(1 for c in t if c.isupper()) >= 2
        )
        is_ext = core.lower() in _CODE_EXTENSIONS
        is_path = "/" in t or "\\" in t
        is_num = core.isdigit()
        if is_camel or is_ext or is_path or is_num:
            codey += 1
    return codey / len(tokens)


def _rrf_lexical_weight_from_shape(shape: float, *, lo: float, hi: float, floor: float) -> float:
    """Map a code-shape fraction onto the lexical-leg weight [floor, 1.0].

    shape <= lo (prose) -> 1.0 (full lexical weight, plain RRF).
    shape >= hi (code/path)-> floor (lean on the semantic leg).
    Linear between. Pure so the fusion math is unit-testable.
    """
    hi = max(hi, lo + 1e-6)
    frac = min(1.0, max(0.0, (shape - lo) / (hi - lo)))
    return 1.0 - frac * (1.0 - floor)


def _rrf_lexical_weight(q: SearchQuery) -> float:
    """Weight for the RRF lexical leg from query composition.

    Returns 1.0 (equal weight) when the request turns adaptive weighting off
    (`rrf_adaptive_lexical=False`) or the query is empty. Otherwise
    code/path-shaped queries down-weight the lexical leg (whose token matches
    on shared file paths are near-noise) so fusion leans semantic; prose keeps
    full weight.
    """
    from server.settings import settings as _settings

    if q.rrf_adaptive_lexical is False or not q.query_text:
        return 1.0
    return _rrf_lexical_weight_from_shape(
        _rrf_code_shape(q.query_text),
        lo=_settings.memory_rrf_code_shape_lo,
        hi=_settings.memory_rrf_code_shape_hi,
        floor=_settings.memory_rrf_lexical_weight_floor,
    )


def _rerank_fact_pool(hits: list[SearchHit], q: SearchQuery) -> list[SearchHit]:
    """Phase 11 of `vector_search`: order the fused pool — by the SQL's
    composite rank_score for the cosine-ordered modes, by reciprocal-rank
    fusion for rrf; lexical and related keep the SQL order."""
    # ANN-eligible modes ordered by the bare value-embedding cosine in
    # SQL (so the HNSW index can fire); re-rank in Python by the
    # composite rank_score the SQL computed. Lexical mode is already
    # composite-ordered in SQL.
    if q.search_mode in ("semantic", "hybrid", "bm25_hybrid", "graph", "temporal"):
        # Key breaks the near-ties. `graph` and `temporal` belong here: unsorted,
        # their fused pool keeps gather order — the ANN fact rows first, the
        # chunk leg's rows appended after them — and the `hits[:top_k]` slice
        # returns facts only. Their rank_score is cosine-based like semantic's,
        # so one sort applies.
        hits.sort(key=hit_sort_key)
    elif q.search_mode == "rrf":
        # RRF: rank lexical and semantic independently, combine via
        # 1/(k + rank), k=60. Ties broken by distance (matters on cold
        # namespaces where many candidates have lex_score=0).
        rrf_k = 60.0
        # Adaptive lexical weight: a code/path-shaped query down-weights the
        # lexical leg, whose matches on shared path tokens are near-noise
        # (`_rrf_lexical_weight`); 1.0 (equal weight) when adaptivity is off.
        w_lex = _rrf_lexical_weight(q)
        # Both rank orders are TOTAL. A plain sort on the lexical score would
        # rank every candidate with lex_score 0 (most of a conversational
        # namespace) by arrival order, so two identical searches could
        # disagree at the window edge. Ties in one leg break by the other
        # leg's score, then key.
        scores = rrf_scores(hits, w_lex=w_lex, k=rrf_k)
        # Only the score changes; `replace` carries every other field of the
        # hit, so a field added to SearchHit cannot go missing under rrf.
        rrf_scored = [
            replace(
                h,
                rank_score=score,
                score_components={
                    **h.score_components,
                    "rrf": float(score),
                    "rrf_lexical_weight": float(w_lex),
                    "rank_score": float(score),
                },
            )
            for h, score in zip(hits, scores, strict=True)
        ]
        rrf_scored.sort(key=hit_sort_key)
        hits = rrf_scored
    return hits
