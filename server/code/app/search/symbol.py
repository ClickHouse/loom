"""Route 1, a symbol-shaped query: the exact symbol-table read, the upper-case
retry, the absent-identifier probe and the identifier scope; and the
fuzzy-symbol vote the hybrid route counts in its route agreement."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.search.shape import _FILE_LINE_RE, _IDENTIFIER_RE
from server.code.app.search.state import CodeSearchResult
from server.code.app.search.window import _exclude
from server.code.app.spans import code_span, span_attrs
from server.code.domain.search_scope import SearchScope
from server.code.domain.units import CodeUnit, budget_units
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


async def _symbol_shaped_route(
    repo: Any,
    org: str,
    ns: str,
    model: str,
    query: str,
    path_prefix: str,
    result: CodeSearchResult,
    work: dict[str, Any],
    *,
    exclude_unit_ids: list[str] | None,
    top_k: int,
    token_budget: int,
    bodies: str,
    pattern_max_files: int | None,
) -> SearchScope | bool | None:
    """Route 1, a symbol-shaped query: the exact symbol-table read first (zero
    embeddings); on a miss, the absent-identifier probe, then the identifier scoping.
    Returns None when `result` is final (answered exactly, or abstained), a
    `SearchScope` when the hybrid legs should run confined to the identifier's chunks,
    False to run them as before."""
    result.route = "symbol_exact"
    work["symbol_queries"] += 1
    with code_span("search.symbol_leg") as sp:
        units = await _symbol_route(repo, org, ns, query)
        span_attrs(sp, results=len(units))
    spelling = query.strip()
    if not units:
        # An all-lower-case identifier is the way a person types a constant
        # (`logical_error` for `LOGICAL_ERROR`); one more keyed symbol read on the
        # upper-case spelling serves the definition exactly instead of scoping the
        # hybrid legs to 1,500 mentioning files.
        folded = _case_folded_symbol_spelling(spelling)
        if folded:
            work["symbol_queries"] += 1
            with code_span("search.symbol_leg", spelling=folded) as sp:
                units = await _symbol_route(repo, org, ns, folded)
                span_attrs(sp, results=len(units))
            if units:
                spelling = folded
    if exclude_unit_ids:
        units = _exclude(units, exclude_unit_ids, work)
    if units:
        for u in units:
            u.via = "symbol"
        with code_span("search.bodies", route="symbol_exact"):
            result.units, result.tokens_returned = budget_units(
                units[:top_k], token_budget, bodies=bodies
            )
        result.retrieval_risk = "low"
        result.retrieval_confidence = 0.95
        result.route_agreement = 1.0
        result.score_explanation = (
            "exact symbol-table match; embeddings not consulted"
            if spelling == query.strip()
            else f"exact symbol-table match on the upper-case spelling {spelling!r}; "
            "embeddings not consulted"
        )
        return None
    absent, probed_files = await _absent_identifier(
        repo, org, ns, model, query, path_prefix, result, work
    )
    if absent:
        return None  # no chunk mentions it
    # The identifier IS mentioned but is no symbol — the hybrid legs are scoped to the
    # chunks that contain it (an unscoped run ranks by its sub-words, `logical` +
    # `error`, and serves include blocks); past the pattern cap that would be a
    # namespace-wide content scan per leg, so the answer is the count and the ops that
    # fit.
    scoped = await _scope_to_identifier(
        repo,
        org,
        ns,
        model,
        query,
        path_prefix,
        result,
        work,
        pattern_max_files,
        probed_files=probed_files,
    )
    if scoped is None and result.abstain:
        return None
    if scoped is not None:
        result.constrained = True
        return scoped
    return False


def _case_folded_symbol_spelling(query: str) -> str:
    """The upper-case spelling to retry an exact symbol read with when the query is a
    bare all-lower-case identifier (`logical_error` → `LOGICAL_ERROR`); '' when the
    query is qualified, a file pointer or mixed case. Only that
    shape is folded: a lower-case word is how people type a SHOUTING constant, while
    `fooBar` or `Foo::bar` already names a spelling."""
    q = query.strip()
    if not q or not _IDENTIFIER_RE.match(q) or not q.islower():
        return ""
    return q.upper()


async def _scope_to_identifier(
    repo: Any,
    org: str,
    ns: str,
    model: str,
    query: str,
    path_prefix: str,
    result: CodeSearchResult,
    work: dict[str, Any],
    pattern_max_files: int | None,
    *,
    probed_files: int | None,
) -> SearchScope | None:
    """After a symbol-table miss on a mentioned identifier: the `SearchScope` that
    confines the hybrid legs to the chunks containing the literal (case-insensitive when
    the query is all lower case — `logical_error` means `LOGICAL_ERROR`), or None. None
    with `result.abstain` set when the identifier appears in more files than the pattern
    cap admits: the hybrid pool would be the whole namespace and the ranking its
    sub-words, so the caller gets the count and a pointer to `code.grep` / a path filter
    / a qualified name instead. `probed_files` is the absent-identifier probe's
    case-sensitive count."""
    probe = _identifier_probe(query)
    if not probe:
        return None
    cap = int(
        settings.code_search_pattern_max_files if pattern_max_files is None else pattern_max_files
    )
    scope = SearchScope(pattern=probe, case_sensitive=not probe.islower())
    present = int(probed_files or 0)
    if not scope.case_sensitive:
        # the probe counted the query's own spelling and the scope admits any case:
        # the scope's own count
        with code_span("search.identifier_scope_count") as sp:
            present = await repo.scope_file_count(org, ns, model, scope, path_prefix=path_prefix)
            span_attrs(sp, results=present)
        work["identifier_scope_files"] = present
    if not present:
        return None
    if present > cap:
        result.route = "symbol_miss_hybrid"
        result.abstain = True
        result.abstain_code = "pattern_too_broad"
        result.abstain_reason = (
            f"{probe!r} is not a symbol and appears in {present} files (cap {cap}): a ranked "
            "list of everything that mentions it would be noise — use code.grep for the "
            "sites, add path_prefix, or search the qualified name that defines it"
        )
        result.retrieval_risk = "abstain"
        result.retrieval_confidence = 0.0
        result.score_explanation = (
            f"symbol table miss; identifier in {present} files, over the pattern cap"
        )
        return None
    result.route = "symbol_miss_scoped"
    result.matched_files = present
    return scope


async def _absent_identifier(
    repo: Any,
    org: str,
    ns: str,
    model: str,
    query: str,
    path_prefix: str,
    result: CodeSearchResult,
    work: dict[str, Any],
) -> tuple[bool, int | None]:
    """After a symbol-table miss, one content pre-count (the constrained search's
    pattern read) settles whether the literal identifier appears in any chunk. Absent,
    the hybrid legs would only serve the chunks its sub-words resemble
    (`_FooService_Bar_Handler` → the hand-written `FooService` handler), so the result
    is filled as an abstain and (True, 0) returned; present, (False, the file count);
    a query with no identifier to probe, (False, None)."""
    result.route = "symbol_miss_hybrid"
    probe = _identifier_probe(query)
    if not probe:
        return False, None
    work["identifier_probe_queries"] = 1
    with code_span("search.identifier_probe") as sp:
        present = await repo.scope_file_count(
            org,
            ns,
            model,
            SearchScope(pattern=probe, case_sensitive=True),
            path_prefix=path_prefix,
        )
        span_attrs(sp, results=present)
    work["identifier_probe_files"] = present
    if present:
        return False, present
    result.route = "identifier_absent"
    result.abstain = True
    result.abstain_code = "identifier_absent"
    result.abstain_reason = (
        f"no indexed file mentions the identifier {probe!r}: it is not defined, called or "
        "named anywhere in this namespace — check the spelling, the branch, or a generated "
        "file the index refuses"
    )
    result.retrieval_risk = "abstain"
    result.retrieval_confidence = 0.0
    result.score_explanation = "symbol table miss; identifier absent from every chunk"
    return True, 0


def _identifier_probe(query: str) -> str:
    """The literal a symbol-shaped query asks about, for the absent-identifier probe:
    the last component of a qualified spelling (`Foo::bar`, `pkg.Name`,
    `a.b.c::Recv.Name` → the bare member), trailing call parentheses dropped; '' when
    what is left is not one identifier."""
    q = query.strip()
    if q.endswith("()"):
        q = q[:-2]
    tail = q.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    return tail if _IDENTIFIER_RE.match(tail) else ""


async def _fuzzy_symbol_vote(
    repo: Any, org: str, ns: str, query: str, work: dict[str, Any]
) -> bool:
    """The fuzzy-symbol leg: votes in route agreement only, never ranks. It needs the
    query alone, so it runs beside the other legs rather than after the slowest one (its
    positionCaseInsensitive scan of code_symbols reads ~111 MiB, 77-170 ms). A failed
    vote is no vote, never a failed search."""
    bare = query.strip().split()[-1] if query.strip() else ""
    if not (bare and len(bare) >= 4 and _IDENTIFIER_RE.match(bare)):
        return False
    work["symbol_queries"] += 1
    with code_span("search.symbol_leg", route="fuzzy_vote") as sp:
        try:
            hit = bool(await repo.symbols_fuzzy(org, ns, bare, limit=1))
        except Exception as e:  # noqa: BLE001 — a vote, never a gate
            logger.warning("code.search fuzzy-symbol vote failed: %s", e)
            hit = False
        span_attrs(sp, results=int(hit))
    return hit


async def _symbol_route(repo: CodeRepo, org: str, ns: str, query: str) -> list[CodeUnit]:
    from server.code.domain.units import unit_from_symbol

    q = query.strip()
    if _FILE_LINE_RE.match(q):
        # file[:line] pointers aren't symbols — let the caller fall through
        # to hybrid, whose lexical leg matches the path tokens.
        return []
    rows = await repo.symbols_by_name(org, ns, q)
    return [unit_from_symbol(r) for r in rows[:10]]
