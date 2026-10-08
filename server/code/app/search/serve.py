"""Serving the answer: the page within the token budget, the history section,
the abstention when nothing fused, and the score explanation."""

from __future__ import annotations

from typing import Any

from server.code.app import history as code_history
from server.code.app.search.routes import _scoped_route
from server.code.app.search.state import _model_name, _SearchState
from server.code.app.search.track import _track
from server.code.app.spans import code_span
from server.code.domain.units import CodeUnit, budget_units
from server.code.store.index import CodeRepo
from server.memory.domain.retrieval_calibration import RetrievalCalibration, finalize_abstain


async def _abstain_no_hits(
    repo: CodeRepo,
    org: str,
    ns: str,
    st: _SearchState,
    *,
    abstain_threshold: float,
    track_path: str,
    embedder: Any,
) -> None:
    """Nothing fused: abstain, keep what the history route knows, and track
    the named path when asked (the caller returns the result)."""
    result, intent, history_section = st.result, st.intent, st.history_section
    constrained, scope, model = st.constrained, st.scope, st.model
    lex_tokens, idfs, qvec = st.lex_tokens, st.idfs, st.qvec
    lexical_rows, vector_rows = st.lexical_rows, st.vector_rows

    if intent is not None and history_section:
        # nothing ranked, but the question was about a change: say what
        # the ledger knows even while abstaining on the code
        history_section["tokens"] = code_history.section_tokens(history_section)
        result.history = history_section
        if history_section.get("prs") or history_section.get("items"):
            result.route = f"history_{intent.kind}"
    result.abstain, result.abstain_code, result.abstain_reason = finalize_abstain(
        top_similarity=None,
        calibration=RetrievalCalibration(0.0, "abstain", 0.0, "no hits"),
        abstain_threshold=abstain_threshold,
        no_match_reason=(
            "no indexed chunk matched the pattern and filters — widen them or grep the working tree"
            if constrained
            else "no strong match in the code index — use grep on the working tree instead"
        ),
    )
    result.retrieval_risk = "abstain"
    if constrained and scope is not None:
        result.route = _scoped_route(result.route, scope)
        result.score_explanation = scope.explain()
    if track_path:
        result.tracked = await _track(
            repo,
            org,
            ns,
            track_path,
            embedder,
            lex_tokens,
            idfs,
            qvec,
            lexical_rows,
            vector_rows,
            served_ids=[],
            model=model,
        )


async def _serve_page(
    repo: CodeRepo,
    org: str,
    ns: str,
    st: _SearchState,
    ranked: list[CodeUnit],
    *,
    top_k: int,
    token_budget: int,
    bodies: str,
    track_path: str,
    embedder: Any,
) -> int:
    """Settle the route, pay for the history section, fill the page's
    bodies within the budget and track the named path; returns the
    section's tokens."""
    result, intent, history_section = st.result, st.intent, st.history_section
    constrained, scope, model = st.constrained, st.scope, st.model
    lex_tokens, idfs, qvec = st.lex_tokens, st.idfs, st.qvec
    lexical_rows, vector_rows, history_rows = st.lexical_rows, st.vector_rows, st.history_rows
    if history_rows:
        result.route += "+history"
    # The history section is paid for inside the budget before any body is
    # filled; the route says which intent produced it (trace: history_number /
    # history_phrase) once the section has something to say.
    section_tokens = 0
    if intent is not None and history_section:
        if history_section.get("prs") or history_section.get("items"):
            suffix = result.route[len("hybrid") :] if result.route.startswith("hybrid") else ""
            result.route = f"history_{intent.kind}" + suffix
        section_tokens = code_history.section_tokens(history_section)
        history_section["tokens"] = section_tokens
        result.history = history_section
    with code_span("search.bodies", route=result.route):
        result.units, result.tokens_returned = budget_units(
            ranked[:top_k], max(200, token_budget - section_tokens), bodies=bodies
        )
        result.tokens_returned += section_tokens
    if constrained and scope is not None:
        result.route = _scoped_route(result.route, scope)
    if track_path:
        result.tracked = await _track(
            repo,
            org,
            ns,
            track_path,
            embedder,
            lex_tokens,
            idfs,
            qvec,
            lexical_rows,
            vector_rows,
            served_ids=[u.unit_id for u in result.units],
            model=model,
        )
    return section_tokens


def _explain_score(
    st: _SearchState,
    *,
    fusion_kind: str,
    legs_mode: str,
    legs_agree: bool,
    section_tokens: int,
    vector_model: str,
    default_vm: str,
    vec_embedder: Any,
    shortlist_n: int,
) -> None:
    """Write `result.score_explanation`: how the answer was reached, from
    the fusion label through every phase's part."""
    result, work, constrained, scope = st.result, st.work, st.constrained, st.scope
    lexical_rows, vector_rows, history_rows = st.lexical_rows, st.vector_rows, st.history_rows
    use_history, history_stats, history_cap = st.use_history, st.history_stats, st.history_cap
    intent, history_section, fuzzy_hit = st.intent, st.history_section, st.fuzzy_hit
    fan_in_explain, fusion_signals = st.fan_in_explain, st.fusion_signals
    kind_explain, tiebreak_applied = st.kind_explain, st.tiebreak_applied
    saturation_explain = st.saturation_explain
    if use_history:
        history_explain = (
            f"history:skipped({history_stats['skipped']}, prs={history_stats['n_prs']})"
            if history_stats["skipped"]
            else f"history:{history_stats['prs']} PRs voted"
            f"({history_stats['files']} files, {len(history_rows)} units, cap={history_cap:g})"
        )
    else:
        history_explain = ""
    if intent is not None:
        n_lift = sum(
            1 for a in history_section.get("anchored") or [] if a.get("placed") == "lifted"
        )
        history_explain = (history_explain + ", " if history_explain else "") + (
            f"history_route:{intent.kind}(prs={len(history_section.get('prs') or [])}, "
            f"items={len(history_section.get('items') or [])}, lifted={n_lift}, "
            f"tokens={section_tokens})"
            + (f", skipped={history_section['skipped']}" if history_section.get("skipped") else "")
            + (", failed" if history_section.get("failed") else "")
        )
    result.score_explanation = (
        f"{fusion_kind}(lexical={len(lexical_rows)}, vector={len(vector_rows)}), "
        f"legs={legs_mode}, legs_agree={legs_agree}, fuzzy_symbol={fuzzy_hit}"
        + (
            f", vector_model={_model_name(vec_embedder)}"
            + (f", shortlist={shortlist_n}" if shortlist_n > 0 else "")
            if (vector_model or default_vm)
            else ""
        )
        + (
            f", {scope.explain()}, matched_files={result.matched_files}"
            if constrained and scope is not None
            else ""
        )
        + (", fusion_signals=on" if fusion_signals else "")
        + _work_explain(work)
        + (", " + fan_in_explain if fan_in_explain else "")
        + (", " + kind_explain if kind_explain else "")
        + (", " + history_explain if history_explain else "")
        + (", user_tiebreak=applied" if tiebreak_applied else "")
        + (", " + saturation_explain if saturation_explain else "")
        + (", " + result.score_explanation_extra if result.score_explanation_extra else "")
    )


def _work_explain(work: dict[str, int]) -> str:
    """score_explanation fragments read off the work counters: the long-question trim."""
    out = ""
    if "query_trimmed_tokens_in" in work:
        out += (
            f", query_trimmed(tokens_in={work['query_trimmed_tokens_in']}, "
            f"tokens_used={work.get('query_trimmed_tokens_used', 0)}, "
            f"boilerplate_lines_dropped={work.get('query_trimmed_boilerplate_lines_dropped', 0)})"
        )
    return out
