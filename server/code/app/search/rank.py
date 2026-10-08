"""Fused rows to ranked units: provenance, then every relevance prior in order,
the rerank and the personal tie-break."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app import fan_in as code_fan_in
from server.code.app.search.fusion import _extra_via_labels, leg_provenance
from server.code.app.search.priors import (
    _apply_fusion_signals,
    _apply_kind_prior,
    _apply_outcome_boost,
    _catalogue_penalty,
    _kind_prior_table,
    _prose_penalty,
    _user_tiebreak,
)
from server.code.app.search.rerank import _rerank_stage
from server.code.app.search.state import _SearchState
from server.code.app.search.window import _diversify
from server.code.app.spans import code_span, span_attrs
from server.code.domain.units import CodeUnit, unit_from_chunk
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


async def _rank_units(
    repo: CodeRepo,
    org: str,
    ns: str,
    st: _SearchState,
    fused: list[tuple[dict[str, Any], float]],
    *,
    query: str,
    top_k: int,
    min_fan_in: int,
    sort_by: str,
    kind_prior: str | None,
    risk_prefetch: Any,
    rerank_endpoint: str,
    user_recent_dirs: list[str] | None,
) -> list[CodeUnit]:
    """Turn the fused rows into units and apply the relevance priors in
    order: provenance, fan-in, fusion signals, kind prior, outcome boost,
    rerank, the personal tie-break. Their explanation parts land on `st`."""
    result, work, constrained, shape = st.result, st.work, st.constrained, st.shape
    lexical_rows, vector_rows, history_rows = st.lexical_rows, st.vector_rows, st.history_rows
    card_rows, concept_rows, hyde_rows = st.card_rows, st.concept_rows, st.hyde_rows
    history_units = st.history_units
    # Provenance BEFORE anchoring and the fusion priors reorder anything:
    # which leg found each chunk and where that leg ranked it.
    prov = leg_provenance(lexical_rows, vector_rows)
    only = _extra_via_labels(prov, history_rows, card_rows, concept_rows, hyde_rows)
    ranked = []
    for row, score in fused:
        u = unit_from_chunk(row, score=score)
        u.via, u.lexical_rank, u.vector_rank = prov.get(u.unit_id, (only.get(u.unit_id, ""), 0, 0))
        ranked.append(u)
    # chunk -> the symbols it defines (a function chunk carries one); the fan-in
    # annotation below maps through it
    symbol_ids_of = {
        str(row.get("chunk_id") or ""): [str(s) for s in (row.get("symbol_ids") or [])]
        for row, _ in fused
    }

    # ---- symbol fan-in: the field on every unit, and the inputs to the optional capped
    # prior, the min_fan_in filter and sort_by='fan_in'. One point read on the stats
    # table's ORDER BY; fails open to zeros (dropped from the wire).
    prior_cap = max(float(settings.code_fan_in_prior_cap), 0.0)
    fan_in_explain = ""
    if ranked:
        with code_span("search.fan_in") as sp:
            n_fan_in = await code_fan_in.annotate_units(repo, org, ns, ranked, symbol_ids_of)
            span_attrs(sp, results=n_fan_in)
        if n_fan_in:
            fan_in_explain = f"fan_in={n_fan_in}"
        if prior_cap > 0:
            fan_in_explain = (fan_in_explain or "fan_in=0") + f"(prior_cap={prior_cap:g})"

    # ---- fusion rank priors: path-noise penalty, query<->path stem affinity, file
    # coherence. Applied to RRF scores BEFORE the outcome boost so both stay
    # multiplicative-capped priors under relevance; calibration already ran on the raw
    # fusion, so abstention is unaffected.
    fusion_signals = False
    if settings.code_fusion_signals:
        ranked = _apply_fusion_signals(
            ranked,
            query,
            prior_cap=prior_cap,
            catalogue_penalty=_catalogue_penalty(work, prose=not constrained and shape != "symbol"),
            prose_penalty=_prose_penalty(eligible=not constrained and shape != "symbol"),
        )
        fusion_signals = True

    # ---- chunk-kind prior (default `flat`): the implementing
    # unit outranks the header that mentions it. Multiplicative, bounded to [0.6, 1.15],
    # skipped when the query names a file or asks about tests; structural phrasing ("the
    # class that ...") prefers the named kind.
    kind_table = _kind_prior_table(kind_prior)
    kind_explain = ""
    if kind_table is not None:
        ranked, kind_explain = _apply_kind_prior(ranked, query, kind_table)
    if risk_prefetch is not None:  # The path reads run beside what follows
        risk_prefetch.start(_diversify(list(ranked), top_k) + history_units)

    # ---- outcome-driven boost (automatic): units that
    # agents actually opened/used rank above units they grep past. Capped
    # multiplicatively so signal can never outrank relevance, decaying with
    # a 14-day half-life so stale popularity fades. Mirrors the memory
    # plane's reinforcement design.
    try:
        signals = await repo.unit_signals(org, ns, [u.unit_id for u in ranked[:100]])
        if signals:
            ranked = _apply_outcome_boost(ranked, signals)
    except Exception as e:  # noqa: BLE001 — ranking signal is best-effort
        logger.debug("outcome boost skipped: %s", e)

    # ---- rerank seam: optional cross-encoder pass over the fused head (the
    # request's endpoint: the top 50; the deployment's kind-gated default: its
    # own top) via the rerank service (builtin:openai / builtin:cohere /
    # operator endpoint). Fail-open: any error keeps the fused order. Under a
    # request budget it is skipped or cut at what is left (rerank.py).
    ranked = await _rerank_stage(
        rerank_endpoint,
        query,
        ranked,
        work,
        result,
        prose=not constrained and shape != "symbol",
    )

    # ---- personalization tie-break ----------------------------------------
    tiebreak_applied = False
    if user_recent_dirs:
        ranked, tiebreak_applied = _user_tiebreak(ranked, user_recent_dirs)

    st.fan_in_explain, st.fusion_signals = fan_in_explain, fusion_signals
    st.kind_explain, st.tiebreak_applied = kind_explain, tiebreak_applied
    return ranked
