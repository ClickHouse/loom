"""code.impact: the multi-hop callers radius of a symbol, a diff or a PR, with its
test map, owners and the verdict of what the radius can vouch for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.code.api.models_symbol import CodeImpactInput, CodeImpactOutput
from server.code.app.common import (
    answer_args,
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    risk_for,
    schedule_code_trace,
    tier_of,
)
from server.code.app.index.pr_overlay import reads_post_image
from server.code.app.index.symbol_forms import resolved_by_of
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.coverage import (
    _declined_safe,
    _env_fields,
    _graph_paths,
    _scip_coverage,
    _unindexed_paths,
)
from server.code.app.symbol.diff import _parse_unified_diff_full, _touched_rows_for_diff
from server.code.app.symbol.dispatch import _dispatch_missing, _dispatch_seeds
from server.code.app.symbol.edges import (
    _IMPACT_READ_LIMIT,
    _expand_impact,
    _hop_counts,
    _overlay_added_rows,
    _pr_overlay,
)
from server.code.app.symbol.resolve import _resolve_symbols, _seed_ids_of
from server.code.domain.index.epistemic import envelope as _envelope
from server.code.domain.index.verdicts import (
    impact_reason,
    impact_verdict,
    is_abstain,
    verdict_is_exact,
)
from server.code.domain.index.verdicts import paths_seen as _paths_seen
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError

logger = logging.getLogger(__name__)


def _test_map_paths(seed_paths: list[str], impacted: dict[str, Any]) -> list[str]:
    """The paths the test-map read sees, in a fixed order: the seeds first, then the
    radius, each sorted. The read cuts at 50 paths and then pages tied rows, so a set's
    per-process iteration order (hash randomisation) would pick both the 50 and the page,
    and the same request would page differently per process. Seeds first so a wide radius
    never cuts the changed files out of the read."""
    seeds = sorted(set(seed_paths))
    radius = {str(v["path"]) for v in impacted.values()} - set(seeds)
    return [*seeds, *sorted(radius)]


def _page_by_hop(
    impacted: dict[str, dict[str, Any]], *, per_hop_limit: int, offset: int
) -> tuple[list[tuple[str, dict[str, Any]]], bool]:
    """Slice the radius PER HOP (`per_hop_limit` rows of each hop from `offset` within
    that hop, hop 1 first, names sorted) so a hub's hop-1 fan-out cannot starve hop 2 of
    the page. Returns (rows, truncated) where truncated says omitted rows exist."""
    by_hop: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for sid, v in impacted.items():
        by_hop.setdefault(int(v["hop"]), []).append((sid, v))
    page: list[tuple[str, dict[str, Any]]] = []
    truncated = False
    for hop in sorted(by_hop):
        rows = sorted(by_hop[hop], key=lambda kv: kv[1]["qualified_name"])
        page.extend(rows[offset : offset + per_hop_limit])
        omitted = len(rows) > offset + per_hop_limit or (offset > 0 and len(rows) > 0)
        truncated = truncated or omitted
    return page, truncated


def _touched_wire(touched_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The touched rows on the wire: the index's rows (first 200) and then every function
    the PR adds (at most 200 more). The added seeds are the rows a pre-merge reader came
    for, so a wide PR's master rows never push them off the list."""
    wire = [
        {
            "qualified_name": str(r["qualified_name"]),
            "path": str(r["path"]),
            "start_line": int(r["start_line"] or 0),
            "tier": str(r["tier"]),
            "via": r["via"],
        }
        for r in touched_rows
    ]
    index_rows = [r for r in wire if r["via"] != "added"][:200]
    return index_rows + [r for r in wire if r["via"] == "added"][:200]


async def _impact_inputs(
    repo: CodeRepo,
    org: str,
    ns: str,
    input: CodeImpactInput,
) -> tuple[Any, list[str], str]:
    """(overlay, files, diff): an open PR's post-image merged over the index for this
    read; with no files / diff of its own, the request is the PR's range (merge-base ..
    head)."""
    overlay: Any = None
    in_files, in_diff = list(input.files), input.diff
    if input.pr_number:
        with code_span("impact.overlay", seeds=input.pr_number) as sp:
            overlay = await _pr_overlay(repo, org, ns, input.pr_number)
            span_attrs(sp, results=len(overlay.symbols) if overlay.ok else 0)
        if overlay.ok and not in_files and not in_diff:
            in_files, in_diff = overlay.changed_paths[:50], overlay.diff[:200_000]
    return overlay, in_files, in_diff


async def _diff_seeds(
    repo: CodeRepo,
    org: str,
    ns: str,
    in_files: list[str],
    in_diff: str,
    overlay: Any,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Diff shape: (seed paths, the index's function rows in them, the touched rows, the
    ids the walk seeds). Hunk ranges narrow whole files to the functions actually edited,
    which is what makes this sharper than a flat file-level reaching set; the functions an
    open PR adds are seeds the index lacks, appended as tier `overlay`."""
    with code_span("impact.diff_parse") as sp:
        hunks, header_names = _parse_unified_diff_full(in_diff) if in_diff else ({}, {})
        span_attrs(sp, results=len(hunks))
    seed_paths = list(dict.fromkeys([*in_files, *hunks.keys()]))[:200]
    with code_span("impact.functions_for_paths", seeds=len(seed_paths)) as sp:
        symbol_rows = (
            await repo.functions_for_paths(org, ns, seed_paths, limit=400) if seed_paths else []
        )
        span_attrs(sp, results=len(symbol_rows))
    touched_rows, seed_ids = _touched_rows_for_diff(symbol_rows, hunks, header_names)
    added_rows = _overlay_added_rows(overlay)
    if added_rows:
        touched_rows = [*touched_rows, *added_rows]
        seed_ids = list(dict.fromkeys([*seed_ids, *(str(r["symbol_id"]) for r in added_rows)]))
        symbol_rows = [*symbol_rows, *(s.row() for s in overlay.symbols)]
    return seed_paths, symbol_rows, touched_rows, seed_ids


@reads_post_image
async def handle_code_impact(input: CodeImpactInput) -> CodeImpactOutput:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    overlay, in_files, in_diff = await _impact_inputs(repo, org, ns, input)

    # A request that names no shape at all (no symbol, no files, an empty diff) is a diff
    # that parsed to nothing: the verdict is `no_match`, not a 404 "symbol not found".
    diff_shaped = bool(in_files or in_diff) or not (input.name or input.symbol_id)
    route = "impact_diff" if in_diff else ("impact_files" if in_files else "impact_symbol")
    if diff_shaped and not (in_files or in_diff):
        route = "impact_diff"
    touched_symbols: list[dict[str, Any]] = []
    co_change_missing: list[dict[str, Any]] = []
    dispatch_n = 0
    dispatch_rows: list[dict[str, Any]] = []
    seeds: list[dict[str, Any]] = []
    symbol_rows: list[dict[str, Any]] = []
    if diff_shaped:
        seed_paths, symbol_rows, touched_rows, seed_ids = await _diff_seeds(
            repo, org, ns, in_files, in_diff, overlay
        )
        seeds = touched_rows
        touched_symbols = _touched_wire(touched_rows)
    else:
        seeds, seed_ids, seed_paths, dispatch_rows, dispatch_n = await _symbol_seeds(
            repo, org, ns, input
        )

    if diff_shaped:
        known_paths, bare_paths = _graph_paths(list(seed_paths), symbol_rows)
    else:
        known_paths, bare_paths = list(seed_paths), []
    with code_span("impact.hop_walk", seeds=len(seed_ids), depth=input.depth) as sp:
        # The envelope's disclosure reads (declined sites named like the seeds; compiler
        # coverage of the seed files) are independent of the walk: gathered with it, they
        # add no round trip. Both shapes size their floor.
        (
            (impacted, capped),
            (unresolved_n, ambiguous_n),
            scip_paths,
            unindexed,
            dispatch_missing,
        ) = await asyncio.gather(
            _expand_impact(
                repo,
                org,
                ns,
                seed_ids,
                input.depth,
                # a page larger than the read cap needs a larger read
                read_limit=max(_IMPACT_READ_LIMIT, input.per_hop_limit),
                overlay=overlay,
            ),
            _declined_safe(repo, org, ns, seeds),
            _scip_coverage(repo, org, ns, known_paths, symbol_rows if diff_shaped else seeds),
            _unindexed_paths(repo, org, ns, bare_paths),
            _dispatch_missing(repo, org, ns, seeds, dispatch_rows, attempted=True)
            if not diff_shaped
            else _false(),
        )
        span_attrs(sp, results=len(impacted))
    by_hop_counts = _hop_counts(impacted)

    impacted_paths = _test_map_paths(seed_paths, impacted)
    with code_span("impact.tests", seeds=len(impacted_paths)) as sp:
        tests = await repo.test_map_for(org, ns, impacted_paths)
        span_attrs(sp, results=len(tests))
    co_changed, co_ranked, owners, reverts = await _pr_history_facts(
        repo, org, ns, seed_paths, diff_shaped=diff_shaped, exclude=tuple(input.exclude_authors)
    )

    if diff_shaped:
        # "You changed these files; these often change WITH them and are absent from this
        # change." Same precision floor as the PR pack (support >= 4 of the PRs considered,
        # confidence >= 15%, at most 5): a looser floor (2-of-50, up to 15 leads) measured
        # 25% precision and a PR reviewer agent chased every lead (+63% cost, findings
        # unchanged); this floor measures ~36%.
        from server.code.app.index.packs import render_co_change_gaps

        co_change_missing = render_co_change_gaps(co_changed, list(seed_paths))

    impacted_sorted, paged = _page_by_hop(
        impacted, per_hop_limit=input.per_hop_limit, offset=input.offset
    )
    truncated = paged or capped
    impacted_rows = [v for _, v in impacted_sorted]
    env = _envelope(
        unresolved=unresolved_n,
        ambiguous=ambiguous_n,
        seed_paths=[*known_paths, *sorted(unindexed)],
        scip_paths=scip_paths,
        unindexed_paths=unindexed,
        dispatch_missing=dispatch_missing,
        truncated=truncated,
    )
    # An empty radius is a verdict, not an abstain. Seeds that resolved and a walk that
    # found nobody is "nothing calls this" (risk by the seeds' tier, as for any graph
    # answer); a change that touches no indexed function is "the graph is untouched". Only
    # a source path the index does not hold, or a diff that parsed to nothing, abstains.
    verdict = impact_verdict(
        seed_paths=list(seed_paths),
        symbol_rows=symbol_rows if diff_shaped else seeds,
        seeds=seeds,
        impacted=impacted,
        unindexed=unindexed,
    )
    # The paths behind the verdict, each with its class, so the consumer can act on
    # `no_indexed_paths` (all `test` = a test-only PR) without re-deriving it. A
    # test/doc/config-only verdict is exact by construction (no function of the graph lives
    # in those paths) whatever the compiler tier covers; the envelope's own reading stands
    # for every other verdict.
    seen_paths = (
        _paths_seen(list(seed_paths), prefixes=_indexed_prefixes(org, ns), unindexed=unindexed)
        if diff_shaped
        else []
    )
    if verdict_is_exact(verdict, seen_paths):
        env.epistemic, env.causes = "exact", []
    abstain = is_abstain(verdict)
    if impacted_rows:
        risk = risk_for(impacted_rows)
    elif abstain:
        risk = "abstain"
    else:
        risk = risk_for(seeds) if seeds else "low"
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.impact",
        # the touched paths, files and diff headers alike, so a diff-only call is
        # replayable from the ledger ("diff" only when there are none)
        query=(input.name or input.symbol_id or ",".join(seed_paths) or "diff"),
        route=route,
        returned_ids=[sid for sid, _ in impacted_sorted],
        returned_paths=[v["path"] for v in impacted_rows],
        returned_scores=[],
        top_k=200,
        tokens_returned=0,
        abstain=abstain,
        retrieval_risk=risk,
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        args=_impact_args(input),
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
        reason=impact_reason(verdict, env.causes, seen_paths),
    )
    set_code_span_attributes(
        route=route,
        tier=tier_of(impacted_rows),
        results=len(impacted_rows),
        seeds=len(seed_ids),
        dispatch_seeds=dispatch_n,
        depth=input.depth,
        abstain=abstain,
        retrieval_risk=risk,
        verdict=verdict,
        consumer=input.consumer,
    )
    out = CodeImpactOutput(
        impacted_symbols=[] if input.summary_only else impacted_rows,
        impacted_tests=[
            {
                "test_path": str(t["test_path"]),
                "src_path": str(t["src_path"]),
                "confidence": float(t["confidence"]),
            }
            for t in tests
        ],
        co_change_files=[{"path": p, "prs": n} for p, n in co_ranked[:20]],
        blast_radius_score=round(min(1.0, len(impacted) / 50.0 + len(tests) / 40.0), 3),
        touched_symbols=touched_symbols,
        co_change_missing=co_change_missing,
        owners=owners,
        reverts=reverts,
        unresolved_calls=unresolved_n,
        ambiguous_calls=ambiguous_n,
        by_hop_counts=by_hop_counts,
        truncated=truncated,
        verdict=verdict,
        paths_seen=seen_paths,
        resolved_by=resolved_by_of(seeds) if not diff_shaped else "",
        **_env_fields(env),
        **(overlay.disclosure() if overlay is not None else {}),
        trace_run_id=trace_run_id,
    )
    out._paths = list(seed_paths)
    return out


async def _symbol_seeds(
    repo: CodeRepo, org: str, ns: str, input: CodeImpactInput
) -> tuple[list[dict[str, Any]], list[str], list[str], list[dict[str, Any]], int]:
    """A symbol-shaped call's seeds: the resolved symbols, their ids plus the ids their
    virtual dispatch adds, their paths, the dispatch rows and how many ids dispatch
    added. A name that resolves to nothing is a 404."""
    with code_span("impact.resolve") as sp:
        seeds = await _resolve_symbols(repo, org, ns, symbol_id=input.symbol_id, name=input.name)
        span_attrs(sp, results=len(seeds), tier=tier_of(seeds))
    if not seeds:
        raise NotFoundError(f"symbol not found: {input.name or input.symbol_id}")
    seed_ids = _seed_ids_of(seeds)
    seed_paths = sorted({str(r["path"]) for r in seeds})
    # Same virtual-dispatch rule as code.callers: an override's blast radius flows
    # through the base declaration's call sites.
    with code_span("impact.dispatch", seeds=len(seeds)) as sp:
        dispatch_rows = await _dispatch_seeds(repo, org, ns, seeds)
        span_attrs(sp, results=len(dispatch_rows))
    dispatch_n = 0
    for d in dispatch_rows:
        if str(d["symbol_id"]) not in seed_ids:
            seed_ids.append(str(d["symbol_id"]))
            dispatch_n += 1
    return seeds, seed_ids, seed_paths, dispatch_rows, dispatch_n


async def _pr_history_facts(
    repo: CodeRepo,
    org: str,
    ns: str,
    seed_paths: list[str],
    *,
    diff_shaped: bool,
    exclude: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[tuple[str, int]], list[Any], list[dict[str, Any]]]:
    """What the seed paths' merged PRs say: the co-changed PRs and the files ranked by how
    often they changed with the seeds, who to ask (authorship and review, topped up with
    the namespace's busiest reviewers), and the recent reverts."""
    with code_span("impact.co_change", seeds=len(seed_paths)) as sp:
        # One PR read serves two consumers: owners want a wide window (hub files' recent PRs
        # are mostly CI robots), co-change keeps the 50/10 it was tuned on; the rows are
        # newest-first, so co-change reads the head of the same window.
        from server.code.domain.index.owners import (
            MAX_OWNERS,
            PR_WINDOW,
            PRIOR_DAYS,
            fill_with_prior,
            rank_owners,
        )

        pr_rows = await repo.pr_history_for(org, ns, paths=seed_paths, limit=PR_WINDOW)
        co_changed = pr_rows[: 50 if diff_shaped else 10]
        span_attrs(sp, results=len(co_changed))
    co_files: dict[str, int] = {}
    for pr in co_changed:
        for f in pr.get("files") or []:
            f = str(f)
            if f not in seed_paths:
                co_files[f] = co_files.get(f, 0) + 1
    co_ranked = sorted(co_files.items(), key=lambda kv: -kv[1])
    # Who to ask: recency-decayed authorship AND review over the seed paths (one reviewer
    # read for the window; on the bench, authorship alone lost to the busiest-reviewers
    # baseline); the change's own author excluded.
    with code_span("impact.reviewers", seeds=len(pr_rows)) as sp:
        by_pr = await repo.reviewers_by_pr(org, ns, [int(pr["pr_number"]) for pr in pr_rows])
        for pr in pr_rows:
            # inline commenters ∪ the people the repo assigned to review (the assignee is
            # the reviewer of record on ClickHouse)
            seen: set[str] = set()
            merged_reviewers: list[str] = []
            assigned = [str(a) for a in (pr.get("assignees") or [])]
            for login in list(by_pr.get(int(pr["pr_number"]), [])) + assigned:
                if login and login.lower() not in seen:
                    seen.add(login.lower())
                    merged_reviewers.append(login)
            pr["reviewers"] = merged_reviewers
        span_attrs(sp, results=sum(len(v) for v in by_pr.values()))
    owners = rank_owners(pr_rows, exclude=exclude)
    # Fewer than three from the area: top up with the namespace's busiest reviewers
    # (bench: hit@3 0.360 → 0.447), labelled via="namespace".
    prior: list[str] = []
    if len(owners) < MAX_OWNERS:
        prior = await repo.top_reviewers(org, ns, days=PRIOR_DAYS, limit=MAX_OWNERS * 2)
    owners = fill_with_prior(owners, prior, exclude=exclude)
    # reverts over the same 200-PR window as owners (newest first, <=5)
    reverts = [
        {
            "pr_number": int(pr["pr_number"]),
            "title": str(pr.get("title") or "")[:120],
            "merged_at": str(pr.get("merged_at") or ""),
        }
        for pr in pr_rows
        if pr.get("is_revert")
    ][:5]
    return co_changed, co_ranked, owners, reverts


def _impact_args(input: CodeImpactInput) -> dict[str, Any]:
    """The impact knobs on the trace row: the request's shape and depth, file and author
    counts, the diff's size."""
    shape = "symbol" if (input.name or input.symbol_id) else ("diff" if input.diff else "files")
    return answer_args(
        input,
        "depth",
        "files",
        "exclude_authors",
        "summary_only",
        "per_hop_limit",
        "offset",
        "pr_number",
        shape=shape,
        diff_bytes=len(input.diff) or None,
    )


def _indexed_prefixes(org: str, ns: str) -> tuple[str, ...]:
    """The repo entry's indexed path prefixes for `paths_seen`'s `unindexed` class; ()
    when the namespace has no entry (nothing is outside an index we cannot describe).
    Never raises: telemetry on a verdict must not fail the verdict."""
    try:
        from server.code.domain.index.repos import repo_for_namespace

        cfg = repo_for_namespace(org, ns)
        return tuple(cfg.path_prefixes) if cfg else ()
    except Exception as e:  # noqa: BLE001
        logger.debug("repo prefixes lookup skipped: %s", e)
        return ()


async def _false() -> bool:
    return False
