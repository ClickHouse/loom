"""The history leg and route: merged-PR text votes for the files each PR
touched, and a question about a change is answered with a `history` section
whose files may anchor the ranking."""

from __future__ import annotations

import logging
import math
from typing import Any

from server.code.app import history as code_history
from server.code.app.search.state import _model_name, _SearchState
from server.code.app.spans import code_span, span_attrs
from server.code.domain.units import CodeUnit, budget_units
from server.code.store.index import CodeRepo
from server.core import bm25
from server.settings import settings

logger = logging.getLogger(__name__)


async def _history_leg(
    repo: CodeRepo,
    org: str,
    ns: str,
    *,
    use_history: bool,
    tokens: list[str],
    work: dict[str, int],
    history_stats: dict[str, Any],
    pr_leg_before: Any,
    path_prefix: str,
    embedder: Any,
) -> list[dict[str, Any]]:
    """The history leg: merged-PR text votes for the files each PR touched; returns
    their chunk rows, voted order first."""
    if not use_history or not tokens:
        return []
    rows: list[dict[str, Any]] = []
    work["history_queries"] = work.get("history_queries", 0) + 1
    with code_span("search.history_leg") as sp:
        try:
            dfs, n_prs = await repo.pr_corpus_stats(
                org,
                ns,
                tokens,
                before=pr_leg_before,
            )
            history_stats["n_prs"] = n_prs
            if n_prs < int(settings.code_search_pr_leg_min_prs):
                history_stats["skipped"] = "sparse"
                return rows
            idfs = [bm25.idf(df, n_prs) for df in dfs]
            prs = await repo.pr_history_search(
                org,
                ns,
                tokens,
                idfs,
                pool=int(settings.code_search_pr_leg_pool),
                before=pr_leg_before,
            )
            history_stats["prs"] = len(prs)
            prefixes = [
                p.strip()
                for p in str(settings.code_search_pr_leg_path_prefixes).split(",")
                if p.strip()
            ]
            voted = file_votes(prs, prefixes)[: int(settings.code_search_pr_leg_max_files)]
            if path_prefix:
                voted = [(f, v) for f, v in voted if f.startswith(path_prefix)]
            history_stats["files"] = len(voted)
            if voted:
                order = {f: i for i, (f, _) in enumerate(voted)}
                rows = await repo.chunks_for_paths(
                    org,
                    ns,
                    _model_name(embedder),
                    [f for f, _ in voted],
                    per_path=int(settings.code_search_pr_leg_units_per_file),
                )
                rows.sort(
                    key=lambda r: (
                        order.get(str(r.get("path")), 1 << 20),
                        int(r.get("start_line") or 0),
                    )
                )
        except Exception as e:  # noqa: BLE001 — history is a vote, never a gate
            logger.warning("code.search history leg failed: %s", e)
            rows = []
        span_attrs(sp, results=len(rows), prs=history_stats["prs"], files=history_stats["files"])
    return rows


async def _history_route_units(
    repo: CodeRepo,
    org: str,
    ns: str,
    st: _SearchState,
    *,
    embedder: Any,
    path_prefix: str,
    pr_leg_before: Any,
) -> list[CodeUnit]:
    """The history route: writes the `history` section to `st.history_section` as soon
    as it is read (so a deadline that drops this leg keeps it) and returns the anchor
    units of the PRs' files."""
    intent, work, tokens, constrained = st.intent, st.work, st.tokens, st.constrained
    if intent is None:
        return []
    work["history_route_queries"] = work.get("history_route_queries", 0) + 1
    max_prs = int(settings.code_search_history_max_prs)
    with code_span("search.history_route", route=f"history_{intent.kind}") as sp:
        try:
            if intent.kind == "number":
                st.history_section = history_section = await code_history.history_for_numbers(
                    repo,
                    org,
                    ns,
                    intent.numbers,
                    max_prs=max_prs,
                )
            else:
                st.history_section = history_section = await code_history.history_for_phrase(
                    repo,
                    org,
                    ns,
                    tokens,
                    max_prs=max_prs,
                    before=pr_leg_before,
                )
            # number intents anchor; phrase intents never do (-26 pp recall@5)
            anchor_on = intent.kind == "number"
            units: list[CodeUnit] = []
            # anchors bypass a content predicate: off under a constraint
            if anchor_on and not constrained and history_section.get("prs"):
                prefixes = [
                    p.strip()
                    for p in str(settings.code_search_pr_leg_path_prefixes).split(",")
                    if p.strip()
                ]
                paths = code_history.anchor_paths(
                    history_section,
                    prefixes,
                    max_files=int(settings.code_search_history_anchor_max_files),
                )
                if path_prefix:
                    paths = [f for f in paths if f.startswith(path_prefix)]
                units = await code_history.anchor_units(
                    repo,
                    org,
                    ns,
                    _model_name(embedder),
                    paths,
                )
        except Exception as e:  # noqa: BLE001 — the section is additive, never a gate
            logger.warning("code.search history route failed: %s", e)
            st.history_section = history_section = {
                "intent": intent.kind,
                "items": [],
                "prs": [],
                "failed": True,
            }
            units = []
        span_attrs(sp, results=len(history_section.get("prs") or []), anchors=len(units))
    return units


def _serve_history_anchors(
    st: _SearchState,
    *,
    top_k: int,
    token_budget: int,
    bodies: str,
) -> None:
    """No leg ranked anything but the history route anchored files: serve
    those files with the section as the answer (the caller returns it)."""
    result, intent, history_section = st.result, st.intent, st.history_section
    history_units = st.history_units

    # The legs found nothing but the ledger did ("#117585" alone): the
    # anchored files ARE the answer; the section says why they are here.
    ranked_h, history_section["anchored"] = code_history.apply_file_anchors(
        [], history_units, top_k
    )
    result.route = f"history_{intent.kind}" if intent is not None else result.route
    section_tokens = code_history.section_tokens(history_section)
    history_section["tokens"] = section_tokens
    result.history = history_section
    with code_span("search.bodies", route=result.route):
        result.units, result.tokens_returned = budget_units(
            ranked_h[:top_k], max(200, token_budget - section_tokens), bodies=bodies
        )
        result.tokens_returned += section_tokens
    result.retrieval_risk = "medium"
    result.retrieval_confidence = 0.5
    result.calibration_explanation = (
        "no content-leg hit; served the files the named change touched (history section)"
    )
    result.score_explanation = (
        f"rrf(lexical=0, vector=0), history_route:{intent.kind if intent else ''}"
        f"(prs={len(history_section.get('prs') or [])}, "
        f"items={len(history_section.get('items') or [])}, "
        f"lifted={len(ranked_h)}, tokens={section_tokens})"
    )


def file_votes(
    prs: list[dict[str, Any]],
    prefixes: list[str] | None = None,
) -> list[tuple[str, float]]:
    """Files ranked by the history's votes. Each PR votes for every file it touched
    under `prefixes` (empty = all) with weight pr_score / log(2 + files_in_pr): a
    48-file sweep says little about any one of its files, a 2-file fix says a lot.
    Deterministic order: vote desc, then path."""
    votes: dict[str, float] = {}
    for pr in prs:
        files = [str(f) for f in (pr.get("files") or [])]
        score = float(pr.get("pr_score") or 0.0)
        if not files or score <= 0.0:
            continue
        weight = score / math.log(2.0 + len(files))
        for f in files:
            if prefixes and not any(f.startswith(p) for p in prefixes):
                continue
            votes[f] = votes.get(f, 0.0) + weight
    return sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
