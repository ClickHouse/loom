"""Fusing the legs' rows into one scored list — reciprocal-rank fusion with the
capped voting lists — and which leg found each chunk."""

from __future__ import annotations

from typing import Any

from server.code.app.search.state import _SearchState
from server.code.app.spans import code_span, span_attrs
from server.settings import settings

_RRF_K = 60.0


def _fuse(st: _SearchState) -> tuple[list[tuple[dict[str, Any], float]], str]:
    """Fuse the legs' rows into one scored list; returns (fused, the fusion label)."""
    lex_weight = st.lex_weight
    fusion_kind = "rrf" if lex_weight == 1.0 else f"rrf(lex_w={lex_weight:g})"
    with code_span("search.fuse", route="rrf") as sp:
        fused = _rrf_fuse(
            st.lexical_rows,
            st.vector_rows,
            st.history_rows,
            history_cap=st.history_cap,
            card_rows=st.card_rows,
            card_cap=float(settings.code_search_card_leg_cap),
            concept_rows=st.concept_rows,
            concept_cap=float(settings.code_search_concept_expand_cap),
            hyde_rows=st.hyde_rows,
            hyde_cap=float(settings.code_search_hyde_cap),
            lexical_weight=lex_weight,
        )
        span_attrs(sp, results=len(fused))
    return fused, fusion_kind


def leg_provenance(
    lexical_rows: list[dict[str, Any]], vector_rows: list[dict[str, Any]]
) -> dict[str, tuple[str, int, int]]:
    """chunk_id -> (via, lexical_rank, vector_rank) from the two leg pools as
    the legs returned them (1-based, first occurrence; 0 = not in that leg).
    via is "both" | "lexical" | "vector"."""
    lex: dict[str, int] = {}
    for i, r in enumerate(lexical_rows, start=1):
        lex.setdefault(str(r["chunk_id"]), i)
    vec: dict[str, int] = {}
    for i, r in enumerate(vector_rows, start=1):
        vec.setdefault(str(r["chunk_id"]), i)
    out: dict[str, tuple[str, int, int]] = {}
    for cid in lex.keys() | vec.keys():
        lr, vr = lex.get(cid, 0), vec.get(cid, 0)
        via = "both" if lr and vr else ("lexical" if lr else "vector")
        out[cid] = (via, lr, vr)
    return out


def _extra_via_labels(
    prov: dict[str, Any],
    history_rows: list[dict[str, Any]],
    card_rows: list[dict[str, Any]],
    concept_rows: list[dict[str, Any]],
    hyde_rows: list[dict[str, Any]],
) -> dict[str, str]:
    """The `via` of a unit neither content leg found: the first voting leg that proposed it —
    history, card, concept, hyde — so the bench's via share and an agent reading `via` see the leg
    instead of ""."""
    out: dict[str, str] = {}
    for label, rows in (
        ("history", history_rows),
        ("card", card_rows),
        ("concept", concept_rows),
        ("hyde", hyde_rows),
    ):
        for r in rows or ():
            cid = str(r["chunk_id"])
            if cid not in prov and cid not in out:
                out[cid] = label
    return out


def _rrf_fuse(
    lexical_rows: list[dict[str, Any]],
    vector_rows: list[dict[str, Any]],
    history_rows: list[dict[str, Any]] | None = None,
    *,
    history_cap: float | None = None,
    card_rows: list[dict[str, Any]] | None = None,
    card_cap: float | None = None,
    concept_rows: list[dict[str, Any]] | None = None,
    concept_cap: float | None = None,
    hyde_rows: list[dict[str, Any]] | None = None,
    hyde_cap: float | None = None,
    lexical_weight: float = 1.0,
) -> list[tuple[dict[str, Any], float]]:
    """Reciprocal-rank fusion, k=60, unchanged for the two content legs. The optional
    history list is fused with the same operator, except that each of its rows
    contributes at most `history_cap` x the top two-leg score: a file the history names
    is lifted among the content legs' candidates, never above the best of them on its
    own."""
    scores: dict[str, float] = {}
    rep: dict[str, dict[str, Any]] = {}
    # The lexical list may weigh less than the vector list (a responsibility question's
    # words are the corpus's common words)
    for rows, weight in ((lexical_rows, float(lexical_weight)), (vector_rows, 1.0)):
        for rank, r in enumerate(rows, start=1):
            cid = str(r["chunk_id"])
            rep.setdefault(cid, r)
            scores[cid] = scores.get(cid, 0.0) + weight / (_RRF_K + rank)
    # the capped lists (history, cards) read the two content legs' top BEFORE either of
    # them adds to it
    top = max(scores.values()) if scores else 1.0 / (_RRF_K + 1.0)
    for extra_rows, cap in (
        (history_rows, history_cap),
        (card_rows, card_cap),
        (concept_rows, concept_cap),
        (hyde_rows, hyde_cap),
    ):
        if not extra_rows:
            continue
        ceiling = float("inf") if cap is None else cap * top
        for rank, r in enumerate(extra_rows, start=1):
            cid = str(r["chunk_id"])
            rep.setdefault(cid, r)
            scores[cid] = scores.get(cid, 0.0) + min(1.0 / (_RRF_K + rank), ceiling)
    fused = sorted(rep.items(), key=lambda kv: -scores[kv[0]])
    return [(row, scores[cid]) for cid, row in fused]
