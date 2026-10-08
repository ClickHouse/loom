"""The routes that run before any leg: route 0 counts a pattern-constrained
pool before it is scanned, route 1 is the exact symbol route; what is left
starts the hybrid legs from a `_SearchState`."""

from __future__ import annotations

from server.code.app.search.shape import classify_query_shape
from server.code.app.search.state import CodeSearchResult, _SearchState
from server.code.app.search.symbol import _symbol_shaped_route
from server.code.app.spans import code_span, span_attrs
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo
from server.memory.domain.retrieval_calibration import RetrievalCalibration, finalize_abstain
from server.settings import settings

_LEGS = ("hybrid", "lexical", "vector")


async def _pattern_pool_gate(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    scope: SearchScope,
    result: CodeSearchResult,
    work: dict[str, int],
    *,
    path_prefix: str,
    pattern_max_files: int | None,
    abstain_threshold: float,
) -> bool:
    """Route 0: count a pattern-constrained pool before scanning it. True
    when `result` already holds the answer (refused as too broad, or an
    empty pool) and the caller returns it without running a leg."""
    cap = int(
        settings.code_search_pattern_max_files if pattern_max_files is None else pattern_max_files
    )
    work["scope_count_queries"] = 1
    with code_span("search.scope_count") as sp:
        matched = await repo.scope_file_count(org, ns, model, scope, path_prefix=path_prefix)
        span_attrs(sp, results=matched)
    result.matched_files = matched
    work["matched_files"] = matched
    if cap > 0 and matched > cap:
        result.route = "hybrid+pattern"
        result.abstain = True
        result.abstain_code = "pattern_too_broad"
        result.abstain_reason = (
            f"pattern matches {matched} files (cap {cap}); narrow the pattern or add "
            "path_globs / langs / path_prefix — the pool was not scanned"
        )
        result.retrieval_risk = "abstain"
        result.score_explanation = f"{scope.explain()}, matched_files={matched}, refused"
        return True
    if matched == 0:
        result.route = "hybrid+pattern"
        result.abstain, result.abstain_code, result.abstain_reason = finalize_abstain(
            top_similarity=None,
            calibration=RetrievalCalibration(0.0, "abstain", 0.0, "no hits"),
            abstain_threshold=abstain_threshold,
            no_match_reason=(
                "no indexed chunk contains the pattern under this scope — widen the "
                "pattern, drop a filter, or grep the working tree"
            ),
        )
        result.retrieval_risk = "abstain"
        result.score_explanation = f"{scope.explain()}, matched_files=0"
        return True
    return False


async def _route_before_legs(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    query: str,
    model: str,
    scope: SearchScope | None,
    result: CodeSearchResult,
    work: dict[str, int],
    legs: str | None,
    extra_terms: list[str] | None,
    path_prefix: str,
    pattern_max_files: int | None,
    abstain_threshold: float,
    exclude_unit_ids: list[str] | None,
    top_k: int,
    token_budget: int,
    bodies: str,
) -> _SearchState | None:
    """Routes 0 and 1: a pattern pool counted before it is scanned, then
    the exact symbol route. None when `result` already holds the answer;
    else the state the legs start from (the symbol route may have scoped
    it to the identifier)."""
    constrained = scope is not None and scope.constrained
    result.constrained = constrained
    shape = classify_query_shape(query)

    # ---- route 0: a constrained pool is counted before it is scanned ------
    # The pattern predicate reads every granule's content under the identity prefix; a
    # pattern that admits more files than the cap would add that scan to the vector leg
    # for nothing, so it is refused with the count and the agent is asked to narrow.
    # Zero files is answered without running either leg.
    if (
        scope is not None
        and scope.has_pattern
        and await _pattern_pool_gate(
            repo,
            org,
            ns,
            model,
            scope,
            result,
            work,
            path_prefix=path_prefix,
            pattern_max_files=pattern_max_files,
            abstain_threshold=abstain_threshold,
        )
    ):
        return None
    # Leg ablation: a per-request A/B switch, never a deployment default. "lexical"
    # skips the vector leg AND the embedding call; "vector" skips the corpus stats and
    # the lexical scan.
    legs_mode = (legs or "hybrid").lower()
    legs_mode = legs_mode if legs_mode in _LEGS else "hybrid"
    result.legs = legs_mode
    run_lexical, run_vector = legs_mode != "vector", legs_mode != "lexical"

    # ---- route 1: symbol-shaped -> exact structural, zero embeddings ------
    # Skipped under a constraint: the symbol table does not carry the
    # predicate, so its definitions could not be checked against it.
    if not constrained and shape == "symbol":
        outcome = await _symbol_shaped_route(
            repo,
            org,
            ns,
            model,
            query,
            path_prefix,
            result,
            work,
            exclude_unit_ids=exclude_unit_ids,
            top_k=top_k,
            token_budget=token_budget,
            bodies=bodies,
            pattern_max_files=pattern_max_files,
        )
        if outcome is None:
            return None  # answered exactly, or abstained (absent / too common)
        if isinstance(outcome, SearchScope):
            scope, constrained = outcome, True  # legs scoped to the identifier
    if legs_mode == "lexical":
        result.route = "lexical_expand" if extra_terms else "lexical_only"
    elif legs_mode == "vector":
        result.route = "vector_only"

    return _SearchState(
        result=result,
        work=work,
        model=model,
        scope=scope,
        constrained=constrained,
        shape=shape,
        legs_mode=legs_mode,
        run_lexical=run_lexical,
        run_vector=run_vector,
    )


def _scoped_route(route: str, scope: SearchScope) -> str:
    """Trace route suffix: `+pattern` when a content predicate narrowed the pool,
    `+scoped` for path filters alone."""
    suffix = "+pattern" if scope.has_pattern else "+scoped"
    return route if route.endswith(suffix) else route + suffix
