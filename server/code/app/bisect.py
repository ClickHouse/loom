"""code.bisect_candidates: the code index and the tracker record, joined at read time,
narrow a bisect to a handful of PRs.

Addressed at a CODE namespace. The crashing frames (qualified symbol names from a stack
trace) resolve to source files through `code_symbols`; the issue number, when given,
supplies the release range from the issue's triage columns in the tracker
(`reproduces_on` / `clean_on`, parsed upstream from the triage comment on the issue) unless
the caller states `bad_version` / `good_version`; the tracker namespace bound to the code
namespace (gate re-run for the current principal) is asked for the merged, non-derivative
PRs whose first release lies in that range and whose files touch the resolved paths, ranked
by the frames their diff names, then the paths, then recency.

Verdicts: `candidates`, `no_candidates` (the range and paths were searched and nothing
touched them; a floor: `affected_file_paths` is what the upstream recorded),
`already_bisected` (the tracker already holds the introducing commit), `graph_named` (a
`caused_by` edge names the PR; the scan is not run), `unknown` with causes
(`tracker_unbound`, `tracker_unavailable`, `no_failing_version`, `no_resolved_paths`).
Epistemic is always `lower_bound`: a culprit that touched a header the frame did not name,
or a PR whose file list the upstream cut, is not excluded by an empty answer.

The candidates carry marks: a backport / cherry-pick / sync title, a revert, a reverted PR
(unless re-landed), a re-land (`reland_of`, never a revert, inheriting the original's symbol
marks), a whitespace-only / rename-only change to the frames' files (one bounded
`git diff -w -U0` per candidate on the mirror), `touches_symbol` from the diff's hunk
headers, the frame named in the title or body. Hard-demoted candidates rank last, and the
symbol marks add a disclosed bonus to the base score. `marks=false` returns the unmarked
candidates unchanged.

A report with no stack frame (a wrong-result bug, roughly half of triaged issues and most
bisected ones) resolves no path, and the scan needs another bound. With `issue_number` it
takes three, each disclosed in `widened_by`: the issue's rarest identifiers (IDF over the
tracker's issue corpus, the bot-written blocks of the body cut first) bound the scan to the
window's PRs whose DIFF carries one (`patterns`); the issue's component widens to the `src/`
directories its PRs touch (`component`); the fix's own files (the record's `fixes` edge,
else the link pass's probable fix) seed the paths (`fix_candidate`). The same IDF weighs
the patterns in the ranking everywhere. `rescan=true` runs the scan although the record
already names a culprit, to check the pin.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any

from server.code.api.models_history import CodeBisectCandidatesOutput
from server.code.app import blame as cb
from server.code.app.bisect_facts import (
    _CUT,
    _DEADLINE,
    RERANK_MIN_S,
    STAGE_MIN_S,
    IssueFacts,
    MarkStage,
    Widening,
    _budget,
    _expired,
    _short,
)
from server.code.app.bisect_fix import (
    _after_scan,
    _candidate_paths,
    _cause_stage,
    _dupcheck_fix,
    _record_fix,
    _resolve_fix,
    _start_dupcheck_fix,
)
from server.code.app.bisect_fusion import (
    fusion_stage,
    rerank_stage,
    start_prefetch,
    wide_extras,
)
from server.code.app.bisect_scan import ScanPlan, _candidate_rows, _plan
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index import merge_shas
from server.code.app.index.mirrors import history_dir
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.domain.index import blame_range as br
from server.code.domain.index.coverage_claim import is_test_file
from server.code.domain.index.repos import default_git_dir
from server.code.domain.time_window import _iso, _parse_when
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.settings import settings
from server.tracker.app import links as lk
from server.tracker.domain import bisect as bi
from server.tracker.domain import bisect_fusion as fu

logger = logging.getLogger(__name__)


_PATHS_PER_SYMBOL = 4


async def _paths_for_symbols(
    repo: CodeRepo, org: str, ns: str, symbols: list[str]
) -> list[dict[str, Any]]:
    """[{symbol, paths}] from the code index; source paths first, tests never.

    A qualified frame whose member is not indexed (a template instantiation, a lambda, an
    inlined or renamed method, e.g. `KeyCondition::cloneDAGWithInversionPushDown`) falls back
    to its owner: the class or namespace before the last `::`, whose definition files are
    where the member lives.
    The row says so (`via: "owner"`); a bare name has no owner to fall back to.
    """

    async def lookup(name: str) -> list[str]:
        try:
            rows = await repo.symbols_by_name(org, ns, name, limit=12)
        except Exception as e:  # noqa: BLE001 — one frame's miss is not the answer
            logger.debug("bisect: symbol lookup failed for %s: %s", name, e)
            rows = []
        paths = [
            str(r.get("path") or "")
            for r in rows
            if r.get("path") and not is_test_file(str(r["path"]))
        ]
        return list(dict.fromkeys(paths))[:_PATHS_PER_SYMBOL]

    async def one(sym: str) -> dict[str, Any]:
        paths = await lookup(sym)
        out: dict[str, Any] = {"symbol": sym, "paths": paths}
        owner = sym.rsplit("::", 1)[0] if "::" in sym else ""
        if not paths and owner and owner != sym:
            paths = await lookup(owner)
            if paths:
                out.update(paths=paths, via="owner", owner=owner)
        return out

    return list(await asyncio.gather(*(one(s) for s in symbols[:12])))


def _edge_prs(edges: list[dict[str, Any]], rel: str, direction: str) -> list[int]:
    from server.tracker.domain.namespaces import split_source_id

    nums = [
        split_source_id(str(e.get("dst_ref") or ""))[1]
        for e in edges
        if e.get("rel_type") == rel
        and e.get("direction") == direction
        and str(e.get("dst_kind") or "").startswith("github_pr")
    ]
    return [n for n in dict.fromkeys(nums) if n > 0][:8]


async def _issue_range(source: Any, org: str, tracker_ns: str, number: int) -> IssueFacts:
    """The issue's facts from its QA row and its edges (the graph is read before any scan; a
    `caused_by` edge names the culprit where the triage columns hold no commit; a `fixes`
    edge names the fix whose files may seed a frameless scan)."""
    from server.tracker.app.facts import fact_of

    rows, edges = await asyncio.gather(
        source.items_by_number(org, tracker_ns, [number]),
        _issue_edges(source, org, tracker_ns, number),
    )
    row = rows.get(number)
    if not row:
        return IssueFacts()
    fact = fact_of(row, edges)
    fact["title"] = str(row.get("title") or "")[:160]
    repro = [str(v) for v in (row.get("reproduces_on") or [])]
    bad = bi.oldest(repro)
    if not bad:
        bad = bi.oldest([str(v) for v in (row.get("affected_versions") or [])])
    good = bi.newest([str(v) for v in (row.get("clean_on") or []) if bi.is_older(str(v), bad)])
    opened = row.get("opened_at") or row.get("created_at")
    return IssueFacts(
        fact=fact,
        bad=bad,
        good=good,
        master=bi.master_reproduces(repro),
        opened_at=opened if isinstance(opened, datetime) else None,
        introduced=str(row.get("introduced_commit") or ""),
        caused=_edge_prs(edges, "caused_by", "out"),
        fixes=_edge_prs(edges, "fixes", "in"),
    )


async def _issue_edges(source: Any, org: str, tracker_ns: str, number: int) -> list[dict[str, Any]]:
    """The issue's `caused_by` and `fixes` edges in one read, or none when
    the graph read fails (the range and the scan still stand on their own)."""
    from server.tracker.domain.namespaces import ref_of

    repos = source.repos()
    # Refs name the tracker's own repositories; there is no fallback to the deployment's.
    refs = [ref_of(r, number) for r in repos]
    if not refs:
        return []
    try:
        # The graph read is a triage step like the diff scan: the first read after a deploy
        # is cold (480–900+ ms) and must not turn a named culprit into `unknown`, so it
        # carries the bisect budget.
        return list(
            await source.edges_from(
                org,
                tracker_ns,
                refs,
                ["caused_by", "fixes"],
                budget_ms=int(settings.tracker_live_bisect_budget_ms),
            )
        )
    except Exception as e:  # noqa: BLE001 — the graph is a bonus, not the range
        logger.info("bisect: issue edges unavailable (%s)", e)
        return []


async def _graph_named(
    source: Any, org: str, tracker_ns: str, caused: list[int]
) -> list[bi.Candidate]:
    """The PRs the graph blames, read by key: the candidates when a `caused_by` edge exists;
    a failed read still names the numbers."""
    from server.tracker.domain.errors import TrackerUnavailable

    with code_span("bisect.graph", seeds=len(caused)):
        try:
            pr_rows = await source.items_by_number(org, tracker_ns, caused)
        except TrackerUnavailable as e:
            logger.info("bisect: graph PR read unavailable (%s)", e)
            pr_rows = {}
    return bi.graph_candidates(pr_rows, caused)


def _diff_targets(
    pool: list[bi.Candidate],
    marks: dict[int, Any],
    records: dict[int, dict[str, Any]],
    *,
    frames_or_paths: bool = True,
) -> list[tuple[bi.Candidate, str]]:
    """Which candidates get one of the bounded diff calls: those the ledger
    knows a merge commit for and no title / body already demoted, newest
    first, at most `code_blame_introduced_max_diff_calls`. None on a frameless pool with no
    paths: a whole-PR diff has no frame to land in and no file to be whitespace-only about."""
    out: list[tuple[bi.Candidate, str]] = []
    if not frames_or_paths:
        return out
    for c in sorted(pool, key=lambda c: (-bi.ordinal_of(c.merged_at), -c.pr_number)):
        sha = br.sha_text((records.get(c.pr_number) or {}).get("merge_commit_sha"))
        m = marks.get(c.pr_number)
        if sha and not (m is not None and m.demoted):
            out.append((c, sha))
    return out[: settings.code_blame_introduced_max_diff_calls]


async def _mark_stage(
    repo: CodeRepo,
    org: str,
    ns: str,
    pool: list[bi.Candidate],
    *,
    frames: list[str],
    paths: list[str],
) -> tuple[list[bi.Candidate], MarkStage]:
    """Mark and re-rank the candidate pool: one ledger read for titles, bodies and merge
    commits; at most `code_blame_introduced_max_diff_calls` bounded diffs of each candidate's
    change to the frames' files on the mirror; the marks folded into the candidates;
    hard-demoted last."""
    stage = MarkStage()
    if not pool:
        return pool, stage
    try:
        records = await repo.pr_records_by_number(org, ns, [c.pr_number for c in pool])
    except Exception as e:  # noqa: BLE001 — the marks are a ranking prior, not the answer
        logger.info("bisect: ledger records unread (%s)", e)
        records, stage.status = {}, "ledger_failed"
    first = bi.mark_bisect_candidates(pool, records=records, frames=frames, diff_kinds={})
    cfg, _siblings = cb.repo_siblings(org, ns)
    git_dir, stage.reason = await cb.history_git_dir(cfg, "code.bisect_candidates")
    targets = (
        _diff_targets(pool, first, records, frames_or_paths=bool(frames or paths))
        if git_dir
        else []
    )
    got = await asyncio.gather(
        *(
            cb.diff_kind(git_dir, sha, tuple(c.matched_paths or paths[:8]), frames)
            for c, sha in targets
        )
    )
    stage.git_calls = sum(1 for _k, _t, called in got if called)
    kinds = {c.pr_number: k for (c, _s), (k, _t, _c) in zip(targets, got, strict=True) if k}
    touch = {
        c.pr_number: t for (c, _s), (_k, t, _c) in zip(targets, got, strict=True) if t is not None
    }
    stage.marks = bi.mark_bisect_candidates(
        pool,
        records=records,
        frames=frames,
        diff_kinds=kinds,
        diff_touch=touch,
    )
    for c in pool:
        c.mark = stage.marks.get(c.pr_number)
    if stage.status == "skipped" and not stage.reason:
        stage.status = "ok"
    return bi.rank_marked(pool, stage.marks), stage


async def _attach_merge_shas(repo: CodeRepo, org: str, ns: str, ranked: list[bi.Candidate]) -> None:
    """Each ranked candidate's merge commit: one ledger read, then one first-parent
    search of the namespace's branch on the mirror for the PRs the ledger lacks."""
    if not ranked:
        return
    numbers = [c.pr_number for c in ranked]
    try:
        stored: dict[int, Any] = await repo.merge_commits_of(org, ns, numbers)
    except Exception as e:  # noqa: BLE001 — the mirror still answers
        logger.info("bisect: ledger merge commits unread (%s)", e)
        stored = {}
    cfg, _siblings = cb.repo_siblings(org, ns)
    # no mirror here yet: the ledger's shas alone (history_dir starts the clone)
    git_dir = (await history_dir(cfg))[0] if cfg is not None else default_git_dir()
    branch = (cfg.git_branch if cfg is not None else "") or settings.code_git_branch
    since = min((br.unix_of(c.merged_at) for c in ranked), default=0)
    got = await merge_shas.resolve(
        git_dir, f"refs/heads/{branch}" if branch else "", numbers, stored, since_unix=since
    )
    for c in ranked:
        c.merge_commit_sha, c.merge_sha_source = got.get(c.pr_number, ("", ""))


async def _vocabulary(source: Any, org: str, tracker_ns: str) -> dict[str, Any]:
    """The issue corpus's vocabulary for the IDF weights (`TrackerSource.token_stats`, kept
    an hour per process). Empty on an outage: the patterns then weigh 1.0 each (plain
    counting)."""
    from server.tracker.domain.errors import TrackerUnavailable

    try:
        return dict(await source.token_stats(org, tracker_ns) or {})
    except TrackerUnavailable as e:
        logger.info("bisect: issue vocabulary unavailable (%s)", e)
        return {}


async def _issue_inputs(
    source: Any,
    org: str,
    tracker_ns: str,
    number: int,
    *,
    frames: list[str],
    patterns: list[str],
) -> IssueFacts:
    """The issue's facts, text and vocabulary; the report's own identifiers join `patterns`,
    rarest × most frequent first. A tracker outage answers empty with qa_ok False."""
    from server.tracker.domain.errors import TrackerUnavailable

    try:
        got, text, stats = await asyncio.gather(
            _issue_range(source, org, tracker_ns, number),
            source.issue_text(org, tracker_ns, number),
            _vocabulary(source, org, tracker_ns),
        )
    except TrackerUnavailable as e:
        logger.info("bisect: issue read unavailable (%s)", e)
        return IssueFacts(qa_ok=False)
    facts: IssueFacts = got
    facts.stats = stats
    facts.text = str(text or "")
    for pat in bi.derive_patterns(text, exclude=[*frames, *patterns], stats=stats):
        if len(patterns) >= 12:
            break
        patterns.append(pat)
    return facts


async def _issue_stage(
    source: Any,
    org: str,
    tracker_ns: str,
    number: int,
    *,
    frames: list[str],
    patterns: list[str],
    bad: str,
    good: str,
    rescan: bool,
) -> tuple[IssueFacts, dict[str, Any] | None]:
    """The issue's inputs and, when neither a `caused_by` edge nor an introducing commit
    names the culprit (or the caller re-scans), the link pass's probable fix above the
    floor. A caller's stated range wins over the issue's."""
    facts = await _issue_inputs(source, org, tracker_ns, number, frames=frames, patterns=patterns)
    facts.bad, facts.good = bad or facts.bad, good or facts.good
    cand = None
    if facts.qa_ok and (rescan or (not facts.caused and not facts.introduced)):
        cand = await lk.fix_candidate(org, tracker_ns, number)
    return facts, cand


async def _widen_frameless(
    source: Any,
    org: str,
    tracker_ns: str,
    facts: IssueFacts,
    *,
    wideners: list[str],
    patterns: list[str],
    weights: dict[str, float],
    fix_candidate: dict[str, Any] | None,
) -> Widening:
    """No frame resolved to a file: the fix's files (the record's `fixes`
    edge, else the link pass's candidate), the component's directories and
    the rarest patterns bound the scan instead, in that order, each named
    in `widened_by` when it contributed."""
    w = Widening(fix=fix_candidate)
    if "fix_candidate" in wideners:
        fix = await _record_fix(source, org, tracker_ns, facts.fixes) if facts.fixes else None
        cand = fix or fix_candidate
        if cand is not None:
            w.paths = await _candidate_paths(source, org, tracker_ns, cand)
            w.fix = cand
            if w.paths:
                w.widened_by.append("fix_candidate")
    component = str(facts.fact.get("component") or "")
    if "component" in wideners and component:
        w.component = component
        w.dirs = await _component_dirs(source, org, tracker_ns, component)
        if w.dirs:
            w.widened_by.append("component")
    k = int(settings.tracker_bisect_scope_patterns)
    if "patterns" in wideners and patterns and k > 0:
        w.scope_patterns = bi.scope_patterns(patterns, weights, k)
        w.widened_by.append("patterns")
    return w


async def _component_dirs(source: Any, org: str, tracker_ns: str, component: str) -> list[str]:
    from server.tracker.domain.errors import TrackerUnavailable

    try:
        rows = await source.component_dirs(
            org,
            tracker_ns,
            component,
            limit=int(settings.tracker_bisect_component_dirs),
            min_lift=float(settings.tracker_bisect_component_min_lift),
        )
    except TrackerUnavailable as e:
        logger.info("bisect: component directories unavailable (%s)", e)
        return []
    return [str(r.get("dir") or "") for r in rows if r.get("dir")]


def _say_where_from(causes: list[str], *, tracker_ns: str, candidates_from: str) -> list[str]:
    """An unbound repository says so (not "unavailable"); candidates from the repository's
    own PR history say that, never read as a tracker answer or a missing one."""
    if not tracker_ns and "tracker_unavailable" in causes:
        causes = ["tracker_unbound" if c == "tracker_unavailable" else c for c in causes]
    if candidates_from == "code_history":
        causes = [c for c in causes if c not in ("tracker_unbound", "tracker_unavailable")]
        causes.append("from_code_history")
    return causes


async def _second_reading(
    input: Any,
    source: Any,
    org: str,
    tracker_ns: str,
    pool: list[bi.Candidate],
    fusion: dict[str, Any],
    window_rows: dict[int, dict[str, Any]],
    *,
    facts: IssueFacts,
    plan: ScanPlan,
    report_text: str,
    report_at: datetime | None,
) -> tuple[list[bi.Candidate], dict[str, Any]]:
    """A weak fused top gets the wider window's candidates and the rerank; a strong one keeps
    the fusion's order (unless `rerank`)."""
    weak = bool(fusion.get("weak"))
    if (weak or input.rerank) and _short(RERANK_MIN_S, "rerank"):
        # the fused order stands; say why it was not read a second time
        return pool, {"status": "skipped", "reason": "deadline", "confidence": "low"}
    extras: list[bi.Candidate] = []
    rows = dict(window_rows)
    if weak:
        extras, extra_rows = await wide_extras(
            source,
            org,
            tracker_ns,
            good=facts.good,
            bad=facts.bad,
            master=facts.master,
            versions=plan.versions,
            report_at=report_at,
            in_window=set(window_rows),
            text=report_text,
            paths=[str(p) for p in input.paths if p],
        )
        rows.update(extra_rows)
    named = list((fusion.get("suspects") or {}).get("named") or [])
    pool, rerank = await rerank_stage(
        pool,
        text=report_text,
        rows_by_number=rows,
        force=bool(input.rerank),
        weak=weak,
        extras=extras,
        cued=named,
        timeout_s=_budget(float(settings.code_bisect_rerank_timeout_s)),
    )
    if rerank.get("status") == "failed" and _expired("rerank"):
        rerank["status"] = "deadline"
    if weak:
        low = (
            rerank.get("status") in ("failed", "deadline")
            or float(rerank.get("top_p") or 0.0) < 0.5
        )
        rerank["confidence"] = "low" if low else "moderate"
        if low:
            fusion["note"] = (
                "Nothing the report carries singles out a candidate: present these as leads "
                "to check, not as the cause."
            )
    return pool, rerank


async def _rank_pool(
    input: Any,
    repo: CodeRepo,
    source: Any,
    org: str,
    ns: str,
    tracker_ns: str,
    pool: list[bi.Candidate],
    *,
    named: list[bi.Candidate],
    facts: IssueFacts,
    plan: ScanPlan,
    frames: list[str],
    patterns: list[str],
    before: datetime | None,
    fix_pr: int,
    prefetch: Any,
    report_text: str,
) -> tuple[list[bi.Candidate], list[bi.Candidate], MarkStage, dict[str, Any], dict[str, Any]]:
    """The scan's pool through its priors and the report-time fusion: (pool, dropped after
    the report, the marks stage, the fusion summary, the fix-blame summary)."""
    dropped: list[bi.Candidate] = []
    if not named:
        # The caller's `before` (the report's time) walls the candidates too, not only the
        # issue's opening time.
        pool, dropped = bi.split_after_report(pool, facts.opened_at or before)
    fusion: dict[str, Any] = {}
    stage = MarkStage()
    if input.marks and pool and not _short(STAGE_MIN_S, "marks"):
        with code_span("bisect.marks", seeds=len(pool)):
            pool, stage = await _mark_stage(repo, org, ns, pool, frames=frames, paths=plan.paths)
    if not named:
        # A later PR's "caused by" reorders the scan signal, and the marked scan order is one
        # signal among the window's (4 of 30 held-out culprits fell behind an unrelated named
        # cause when it led the answer).
        if source is not None and facts.qa_ok and pool:
            pool = await _cause_stage(
                source, org, tracker_ns, pool, paths=plan.paths, frames=frames, patterns=patterns
            )
        pool, fusion, window_rows = await fusion_stage(
            prefetch,
            pool,
            report_at=facts.opened_at or before,
            before=plan.before,
            text=report_text,
            paths=[str(p) for p in input.paths if p],
            issue_number=int(input.issue_number or 0),
        )
        if fusion.get("window_prs") and stage.marks:
            pool = fu.keep_fused_order(pool, stage.marks)
        pool, rerank = await _second_reading(
            input,
            source,
            org,
            tracker_ns,
            pool,
            fusion,
            window_rows,
            facts=facts,
            plan=plan,
            report_text=report_text,
            report_at=facts.opened_at or before,
        )
        if rerank:
            fusion["rerank"] = rerank
    pool, blame_summary = await _after_scan(
        repo,
        source,
        org,
        ns,
        tracker_ns,
        pool,
        fix_pr,
        facts,
        named=bool(named),
        paths=plan.paths,
        frames=frames,
        patterns=patterns,
        causes=not fusion.get("window_prs"),
    )
    if fusion.get("window_prs"):
        pool = fu.lead_suspects(pool, list((fusion.get("suspects") or {}).get("named") or []))
    return pool, dropped, stage, fusion, blame_summary


async def _named_or_prefetch(
    input: Any,
    source: Any,
    org: str,
    tracker_ns: str,
    facts: IssueFacts,
    *,
    early: Any,
    introduced: str,
    before: Any,
    report_text: str,
) -> tuple[list[bi.Candidate], Any]:
    """The graph's named cause, else the fusion's window reads started early.

    The prefetch starts before the named read, beside the duplicate check; a named cause
    makes it moot, so it is cancelled.
    """
    prefetch = (
        None
        if introduced
        else start_prefetch(
            source,
            org,
            tracker_ns,
            versions=early.versions,
            before=early.before,
            report_at=facts.opened_at or before,
            text=report_text,
        )
    )
    named: list[bi.Candidate] = []
    if source is not None and facts.qa_ok and facts.caused and not introduced and not input.rescan:
        try:
            named = await _graph_named(source, org, tracker_ns, facts.caused)
        except BaseException:
            if prefetch is not None:
                prefetch.cancel()  # the caller never receives it to cancel
            raise
    if named and prefetch is not None:
        prefetch.cancel()
        prefetch = None
    return named, prefetch


async def handle_code_bisect_candidates(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    deadline_ms = input.deadline_ms
    deadline_token = _DEADLINE.set(started + deadline_ms / 1000 if deadline_ms else None)
    cut_token = _CUT.set([])
    try:
        return await _handle_bisect(input, started)
    finally:
        _DEADLINE.reset(deadline_token)
        _CUT.reset(cut_token)


async def _handle_bisect(input: Any, started: float) -> Any:
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.app.source import tracker_source

    org, ns = resolve_code_identity(input.org, input.namespace)
    if not (
        input.symbols
        or input.paths
        or input.issue_number
        or input.fix_pr
        or str(input.text or "").strip()
    ):
        # Checked after the gate, so a refused principal never learns the input shape. The
        # report's text alone is enough: it ranks the window.
        raise ValidationError("name at least one of symbols, paths, issue_number, fix_pr, text")
    repo = CodeRepo()
    # The tracker only enriches the answer, unless the caller named its issue or fix.
    named = bool(input.issue_number or input.fix_pr)
    tracker_ns = await authorized_tracker_namespace(org, ns, hides_answer=named)
    source = tracker_source(org, tracker_ns) if tracker_ns else None
    before, after = await _time_window(input, org, ns)
    facts = IssueFacts(
        # a `10.6` lifted from a report body is not a release
        bad=fu.sane_version(bi.minor_of(input.bad_version), bi.parse_version),
        good=fu.sane_version(bi.minor_of(input.good_version), bi.parse_version),
        qa_ok=source is not None,
        before=before,
        after=after,
    )
    frames = bi.frame_names(list(input.symbols))
    patterns = [str(p).strip() for p in input.patterns if str(p).strip()][:12]
    owned: list[asyncio.Future[Any]] = []
    dup_task = _start_dupcheck_fix(input, org, ns, frames)
    if dup_task is not None:
        owned.append(dup_task)
    try:
        return await _run_bisect(
            input,
            repo,
            source,
            org,
            ns,
            tracker_ns,
            facts,
            frames=frames,
            patterns=patterns,
            window=(before, after),
            started=started,
            dup_task=dup_task,
            owned=owned,
        )
    finally:
        # The duplicate check and the fusion's window reads run as tasks beside the scan: an
        # early failure or a cancelled call must not leave them reading.
        for task in owned:
            if not task.done():
                task.cancel()


async def _run_bisect(
    input: Any,
    repo: CodeRepo,
    source: Any,
    org: str,
    ns: str,
    tracker_ns: str,
    facts: IssueFacts,
    *,
    frames: list[str],
    patterns: list[str],
    window: tuple[Any, Any],
    started: float,
    dup_task: Any,
    owned: list[asyncio.Future[Any]],
) -> Any:
    """The bisect after the gate and the window: resolve, issue stage, widening, the named
    cause or the fusion prefetch (added to `owned`), the scan, the ranking and the answer."""
    before, after = window
    fix_candidate: dict[str, Any] | None = None
    with code_span("bisect.resolve", seeds=len(input.symbols)):
        resolved = await _paths_for_symbols(repo, org, ns, list(input.symbols))
        if source is not None and input.issue_number:
            facts, fix_candidate = await _issue_stage(
                source,
                org,
                tracker_ns,
                int(input.issue_number),
                frames=frames,
                patterns=patterns,
                bad=facts.bad,
                good=facts.good,
                rescan=bool(input.rescan),
            )
    weights = bi.pattern_weights(patterns, facts.stats) if facts.stats else {}
    widening = Widening(fix=fix_candidate)
    frameless = not input.paths and not any(r["paths"] for r in resolved)
    if frameless and source is not None and facts.qa_ok and input.issue_number:
        widening = await _widen_frameless(
            source,
            org,
            tracker_ns,
            facts,
            wideners=list(input.wideners),
            patterns=patterns,
            weights=weights,
            fix_candidate=fix_candidate,
        )
    facts.before, facts.after = before, after  # the issue stage rebuilds facts; the window stays
    # a re-scan ignores what the record already names; the answer still carries it
    introduced = "" if input.rescan else facts.introduced
    report_text = str(input.text or facts.text or "")
    # The fusion's window reads run beside the diff scan and the duplicate check, so they
    # start now: the window needs no fix.
    named, prefetch = await _named_or_prefetch(
        input,
        source,
        org,
        tracker_ns,
        facts,
        early=_plan(
            input,
            resolved=resolved,
            widening=widening,
            frames=frames,
            patterns=patterns,
            facts=facts,
        ),
        introduced=introduced,
        before=before,
        report_text=report_text,
    )
    if prefetch is not None:
        owned.append(prefetch)
    fix_pr, widening = await _resolve_fix(input, facts, widening, source, org, tracker_ns)
    plan = _plan(
        input, resolved=resolved, widening=widening, frames=frames, patterns=patterns, facts=facts
    )
    scannable = bool(plan.paths or plan.widened) and (bool(plan.versions) or plan.timed)
    rows, facts.qa_ok, candidates_from = await _candidate_rows(
        source,
        org,
        ns,
        tracker_ns,
        plan,
        facts,
        scan=scannable and not introduced and not named,
        frames=frames,
        patterns=patterns,
        weights=weights,
        limit=input.limit,
    )
    pool = named or bi.rank_candidates(
        rows,
        frames=frames,
        paths=plan.paths,
        limit=max(input.limit, len(rows)),
        patterns=patterns,
        pattern_weights=weights,
    )
    # The check ran beside the resolve and the scan; its fix feeds the ranking stages (the
    # fix's blame and cause statement), not the scan's scope, so nothing above waited on it.
    dup_fix = await _dupcheck_fix(dup_task, started)
    if dup_fix.get("fix_pr") and not fix_pr:
        fix_pr = int(dup_fix["fix_pr"])
        widening.widened_by = [*widening.widened_by, "dupcheck_fix"]
    pool, dropped, stage, fusion, blame_summary = await _rank_pool(
        input,
        repo,
        source,
        org,
        ns,
        tracker_ns,
        pool,
        named=named,
        facts=facts,
        plan=plan,
        frames=frames,
        patterns=patterns,
        before=before,
        fix_pr=fix_pr,
        prefetch=prefetch,
        report_text=report_text,
    )
    ranked = pool[: input.limit]
    await _attach_merge_shas(repo, org, ns, ranked)
    verdict, causes = bi.verdict_of(
        candidates=ranked,
        versions=plan.versions,
        paths=plan.paths,
        introduced_commit=introduced,
        time_window=plan.timed,
        qa_ok=facts.qa_ok,
        graph_named=bool(named),
        widened=plan.widened,
    )
    causes = _say_where_from(causes, tracker_ns=tracker_ns, candidates_from=candidates_from)
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.bisect_candidates",
        query=" ".join(frames) or " ".join(plan.paths[:5]) or " ".join(plan.scope_patterns) or "-",
        route="bisect",
        returned_ids=[str(c.pr_number) for c in ranked],
        returned_paths=plan.paths[:20],
        returned_scores=[c.score for c in ranked],
        top_k=input.limit,
        tokens_returned=0,
        abstain=verdict == bi.UNKNOWN,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(route="bisect", results=len(ranked), consumer=input.consumer)
    cut = list(_CUT.get() or [])
    return CodeBisectCandidatesOutput(
        verdict=verdict,
        causes=causes,
        epistemic="lower_bound",
        candidates=[c.as_dict() for c in ranked],
        patterns=patterns,
        pattern_weights=weights,
        scope=plan.scope,
        widened_by=list(widening.widened_by),
        dupcheck_fix=dup_fix or {},
        component=widening.component,
        component_dirs=list(widening.dirs),
        dropped_after_report=len(dropped),
        versions_searched=plan.versions,
        bad_version=facts.bad,
        good_version=facts.good,
        window=bi.window_name(facts.good, facts.bad, plan.versions, before=plan.timed),
        candidates_from=candidates_from,
        before=_iso(plan.before) if plan.timed else "",
        after=_iso(plan.after) if plan.timed and plan.after else "",
        hub_paths_dropped=plan.hub_dropped,
        resolved=resolved,
        paths=plan.paths,
        frames=frames,
        issue=facts.fact,
        introduced_commit=facts.introduced,
        fix_candidate=widening.fix,
        named_by_graph=[c.pr_number for c in named],
        fix_blame=blame_summary,
        fusion=fusion,
        marks=stage.as_dict(),
        tracker_namespace=tracker_ns,
        tracker_unavailable=bool(tracker_ns) and not facts.qa_ok,
        trace_run_id=trace_run_id,
        partial=bool(cut),
        partial_reason=f"deadline_ms cut: {', '.join(cut)}" if cut else "",
    )


# --- a merge-time window ------------------------------------------------------


async def _time_window(input: Any, org: str, ns: str) -> tuple[datetime | None, datetime | None]:
    """(before, after) for the scan when the caller gave times or a bad
    commit; (None, None) when releases bound it. `bad_sha` resolves on the
    namespace's mirror to the commit's time; a sha the mirror does not have
    is a sentence, not an empty window."""
    before = _parse_when(str(getattr(input, "before", "") or ""))
    after = _parse_when(str(getattr(input, "after", "") or ""))
    sha = str(getattr(input, "bad_sha", "") or "").strip()
    if sha and before is None:
        from server.code.app.index import gitrepo
        from server.code.domain.index.repos import repo_for_namespace
        from server.core.errors import ValidationError

        cfg = repo_for_namespace(org, ns)
        if cfg is None:
            raise ValidationError(
                f"{ns} has no repository mirror to resolve {sha!r}", code="BAD_SHA"
            )
        git_dir, gap = await history_dir(cfg)
        if gap:
            raise ValidationError(f"{gap}; pass `before` as a time instead", code="BAD_SHA")
        try:
            epoch = await gitrepo.commit_time(git_dir, sha)
        except Exception:  # noqa: BLE001 — unknown sha, git error
            raise ValidationError(
                f"commit {sha!r} is not on the {ns} mirror; pass `before` as a time instead",
                code="BAD_SHA",
            ) from None
        before = datetime.fromtimestamp(int(epoch), tz=UTC).replace(tzinfo=None)
    if before is not None and after is not None and after >= before:
        from server.core.errors import ValidationError

        raise ValidationError("`after` must be earlier than `before`", code="BAD_TIME")
    return before, after
