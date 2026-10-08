"""Route 2's evidence: plan the query, configure every leg and run them beside
each other under the request's budget."""

from __future__ import annotations

from typing import Any

from server.code.app import history as code_history
from server.code.app.search.history import _history_leg, _history_route_units
from server.code.app.search.hyde import _hyde_config
from server.code.app.search.lexical import _lexical_leg
from server.code.app.search.state import _SearchState
from server.code.app.search.symbol import _fuzzy_symbol_vote
from server.code.app.search.vector import _QueryVecMemo, _vector_leg_rows
from server.code.app.search.voting import _voting_leg_config, _voting_leg_rows
from server.code.app.search_legs import MODEL_LEG_MIN_MS, leg_window, llm_legs_off, run_legs
from server.code.domain.index import query_trim
from server.code.store.index import CodeRepo
from server.settings import settings


async def _gather_legs(
    repo: CodeRepo,
    org: str,
    ns: str,
    st: _SearchState,
    *,
    query: str,
    embedder: Any,
    vec_embedder: Any,
    shortlist_n: int,
    run_lexical: bool,
    run_vector: bool,
    path_prefix: str,
    extra_terms: list[str] | None,
    pr_leg_before: Any,
    llm_legs: str | None,
) -> None:
    """Route 2's evidence: plan the query, configure every leg, and run
    them beside each other; the legs' rows land on `st`. Under a request
    budget the legs get what it has left less the post-leg reserve."""
    work, scope, constrained, shape, model = st.work, st.scope, st.constrained, st.shape, st.model
    # extra_terms extend the LEXICAL leg only (code.ask query expansion: proposed
    # identifiers bridge natural-language -> codebase vocabulary; the vector leg keeps
    # embedding the user's actual question). A long question is asked with its rarest
    # words and its cleaned first page
    trim = query_trim.plan(query, extra_terms, settings.code_search_query_max_tokens, work)
    tokens = st.tokens = trim.tokens
    # one query embedding per embedder per call: _QueryVecMemo
    _query_vec = _QueryVecMemo(trim.embed_text, work)

    # The two legs are independent reads — the lexical leg needs the corpus stats for
    # its idf, the vector leg needs the query embedding — so they run together; the
    # fusion reads both results.

    # The history leg — merged-PR text scored lexically votes for the files each PR
    # touched; the top voted files map to their chunk units and join the fusion as a
    # third RRF list. Runs beside the other two legs (no embedding, three small reads),
    # gated by shape or forced.
    pr_mode = settings.code_search_pr_leg.lower()
    use_history = st.use_history = pr_mode == "always" or (pr_mode == "issue" and shape == "issue")
    st.history_cap = float(settings.code_search_pr_leg_cap)
    history_stats = st.history_stats = {"prs": 0, "files": 0, "n_prs": 0, "skipped": ""}

    # A question ABOUT a change ("what fixed issue 117585", "PR #114783", "why was X
    # changed") is answered from the PR plane as a `history` section; the PRs' files may
    # anchor the ranking below through the rank-lift shape. Number mentions are three
    # point reads; phrasings reuse the history leg's statement with a pool of N.
    hist_mode = settings.code_search_history_route.lower()
    intent = code_history.classify_history_intent(query) if hist_mode != "off" else None
    if intent is not None and intent.kind == "phrase" and hist_mode != "on":
        intent = None
    st.intent = intent
    st.history_section = {}

    # The card leg — the weekly file cards nearest the query vote for their files; the
    # files' skeleton chunks join the fusion as a capped RRF list (the history leg's
    # shape). Prose-only: a constrained scope (pattern / paths) cannot be honoured on
    # cards, and a symbol or path spelled in the query is the chunk legs' job. Shares
    # the vector leg's query embedding (one provider call; `_query_vec` memoises it).
    use_cards, st.card_gate, concept_model, st.lex_weight = _voting_leg_config(
        query,
        run_lexical=run_lexical,
        work=work,
        llm_legs=llm_legs,
        prose=(not constrained and embedder is not None and run_vector and shape != "symbol"),
    )
    st.card_stats = {"files": 0, "top_similarity": None}
    st.concept_stats = {"terms": []}
    hyde_prose = embedder is not None and not constrained
    leg_ms, floor_ms, llm_legs = _leg_budget(st, llm_legs, prose=hyde_prose)

    card_legs, hyde_leg = _voting_leg_rows(
        repo,
        org,
        ns,
        model,
        query=query,
        query_vec=lambda: _query_vec(embedder),
        use_cards=use_cards,
        card_gate=st.card_gate,
        concept_model=concept_model,
        path_prefix=path_prefix,
        scope=scope if constrained else None,
        embedder=embedder,
        work=work,
        card_stats=st.card_stats,
        concept_stats=st.concept_stats,
        llm_legs=llm_legs,
    )
    # the fuzzy-symbol vote needs the query alone, so it runs beside the legs; under a
    # deadline a late leg is dropped and named
    (
        (st.lexical_rows, st.lex_tokens, st.idfs),
        (st.vector_rows, st.qvec),
        st.history_rows,
        st.history_units,
        st.fuzzy_hit,
        (st.card_rows, st.concept_rows),
        st.hyde_rows,
    ) = await _run_legs_releasing(
        _query_vec,
        {
            "lexical": (
                _lexical_leg(
                    repo,
                    org,
                    ns,
                    model,
                    run_lexical=run_lexical,
                    tokens=tokens,
                    trim=trim,
                    work=work,
                    path_prefix=path_prefix,
                    scope=scope if constrained else None,
                ),
                ([], tokens, []),
            ),
            "vector": (
                _vector_leg_rows(
                    repo,
                    org,
                    ns,
                    model,
                    embedder=embedder,
                    vec_embedder=vec_embedder,
                    run_vector=run_vector,
                    shortlist_n=shortlist_n,
                    path_prefix=path_prefix,
                    scope=scope if constrained else None,
                    query_vec=_query_vec,
                    work=work,
                ),
                ([], None),
            ),
            "history": (
                _history_leg(
                    repo,
                    org,
                    ns,
                    use_history=use_history,
                    tokens=tokens,
                    work=work,
                    history_stats=history_stats,
                    pr_leg_before=pr_leg_before,
                    path_prefix=path_prefix,
                    embedder=embedder,
                ),
                [],
            ),
            "history_route": (
                _history_route_units(
                    repo,
                    org,
                    ns,
                    st,
                    embedder=embedder,
                    path_prefix=path_prefix,
                    pr_leg_before=pr_leg_before,
                ),
                [],
            ),
            "symbol_vote": (_fuzzy_symbol_vote(repo, org, ns, query, work), False),
            "card": (card_legs, ([], [])),
            "hyde": (hyde_leg, []),
        },
        result=st.result,
        work=work,
        deadline_ms=leg_ms,
        floor_ms=floor_ms,
    )
    # A content leg that raised answered nothing: the result is as partial as one
    # a deadline cut, never a complete "no match".
    if work.get("lexical_leg_failed") or work.get("vector_leg_failed"):
        st.result.epistemic = "partial"


async def _run_legs_releasing(
    query_vec: _QueryVecMemo, legs: dict[str, Any], **kw: Any
) -> list[Any]:
    """`run_legs`, then the query embedding released: a deadline that dropped
    every leg awaiting it leaves the shielded provider call running with no
    owner, and its error, if any, unretrieved."""
    try:
        return await run_legs(legs, **kw)
    finally:
        query_vec.release()


def _leg_budget(
    st: _SearchState, llm_legs: str | None, *, prose: bool
) -> tuple[int | None, int | None, str | None]:
    """(leg deadline ms, floor ms, llm_legs) under the request's budget: a model-backed
    leg that would run is skipped (`llm_legs="none"`) when the legs' share cannot pay
    for it."""
    budget = st.result.budget
    leg_ms, floor_ms = leg_window(budget)
    if budget is None or leg_ms is None or leg_ms >= MODEL_LEG_MIN_MS or llm_legs_off(llm_legs):
        return leg_ms, floor_ms, llm_legs
    if _hyde_config(st.work, prose=prose):
        budget.drop("hyde skipped")
    return leg_ms, floor_ms, "none"
