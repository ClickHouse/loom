"""The bisect fix stages: the fix a report names or the duplicate check finds, the
cause blamed from it, and the widening the fix's paths give the scan."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.code.api.models_history import CodeBlameRangeInput
from server.code.app.bisect_facts import (
    _MAX_PATHS,
    STAGE_MIN_S,
    IssueFacts,
    Widening,
    _budget,
    _expired,
    _short,
)
from server.code.app.spans import code_span
from server.code.domain.index import blame_introduced as intro
from server.code.store.index import CodeRepo
from server.settings import settings
from server.tracker.domain import bisect as bi
from server.tracker.domain import bisect_fusion as fu

logger = logging.getLogger(__name__)


# The pool head whose cause statements one tracker read checks
CAUSE_SCAN_TOP = 25


async def _cause_stage(
    source: Any,
    org: str,
    tracker_ns: str,
    pool: list[bi.Candidate],
    *,
    paths: list[str],
    frames: list[str],
    patterns: list[str],
) -> list[bi.Candidate]:
    """A later PR whose description names a pool candidate as the cause, and whose change is
    about this bug, puts that candidate first with the naming PR disclosed. One bounded
    tracker read over the top of the pool; an outage or an empty answer keeps the pool as it
    was."""
    read = getattr(source, "cause_statements", None)
    head = pool[:CAUSE_SCAN_TOP]
    since = min((c.merged_at for c in head if c.merged_at), default="")
    if read is None or not since:
        return pool
    try:
        with code_span("bisect.causes", seeds=len(head)):
            stmts = await read(
                org, tracker_ns, [c.pr_number for c in head], since=since[:19].replace("T", " ")
            )
    except Exception as e:  # noqa: BLE001 — a ranking prior, never the answer
        logger.info("bisect: cause statements unavailable (%s)", e)
        return pool
    if not stmts:
        return pool
    top = bi.promote_named_causes(head, stmts, paths=paths, frames=frames, patterns=patterns)
    return top + pool[CAUSE_SCAN_TOP:]


FIX_BLAME_CONCURRENCY = 4


def _start_dupcheck_fix(input: Any, org: str, ns: str, frames: list[str]) -> Any:
    """A report with no `fix_pr` and no issue gets the duplicate check on its own text,
    started beside the resolve reads. None when the caller named a fix or an issue, the text
    is empty, or the report itself names a suspect PR (never overridden)."""

    text = str(input.text or "").strip()
    if (
        input.fix_pr
        or input.issue_number
        or not text
        or not bi.parse_version(input.bad_version)
        or fu.report_suspects(text)[0]
    ):
        return None

    async def run() -> dict[str, Any]:
        from server.tracker.api.models import TrackerDuplicateCheckInput, within_item_bound
        from server.tracker.app import dupcheck as tdc
        from server.tracker.app import dupcheck_links as dl

        first, _, rest = text.partition("\n")
        title, body = first.strip()[:500], rest.strip()[:20000]
        # a check the session already ran on this report costs nothing
        cached = None if input.before else tdc.recent_answer(org, ns, title, body)
        out = (
            cached
            or await tdc.handle_tracker_duplicate_check(
                TrackerDuplicateCheckInput(
                    org=org,
                    namespace=ns,
                    title=title,
                    body=body,
                    frames=within_item_bound(frames)[:60],
                    paths=within_item_bound(input.paths)[:40],
                    related_prs=False,
                    rerank=False,  # the legs alone: the rerank's second is the budget
                    as_of=str(input.before or "")[:40],  # a replay sees what the report could
                )
            )
        )
        d = out.model_dump()
        row = dl.lead_issue(d)
        pr = dl.merged_trunk_fix(row) if row is not None else 0
        if not pr or row is None or not _fix_after_regression(row, input.bad_version):
            return {}
        issue = int(row.get("number") or 0)
        return {
            "status": "cached" if cached is not None else "matched",
            "issue": issue,
            "fix_pr": pr,
            "match": "duplicate",
            "namespace": str(row.get("namespace") or ""),
            "note": f"the report is a duplicate of #{issue}, fixed by #{pr}; the candidates below "
            "lead with what that fix's changed lines and cause statement point at",
        }

    return asyncio.ensure_future(run())


def _fix_after_regression(row: dict[str, Any], bad_version: str) -> bool:
    """A matched issue's fix can be this regression's fix only if it ships after the failing
    release: a fix `bad` already carries fixes some older bug (without this check, older
    look-alike issues and their old fixes displaced the culprit, held-out top-1 10 → 8). A
    fix no release carries yet is newer than any. No failing release named: nothing to check
    against, no fix."""
    bad = bi.parse_version(bad_version)
    if row.get("fixed_in_source") == "not_released":
        return bad is not None
    ships = bi.parse_version(str(row.get("fixed_in") or ""))
    # Strictly after: a fix the failing release already carries did not fix a bug that
    # reproduces on it (the culprit itself can be an earlier issue's "fix" shipped in the
    # failing release).
    return bad is not None and ships is not None and ships > bad


async def _dupcheck_fix(task: Any, started: float | None = None) -> dict[str, Any]:
    """The matched issue's fix, or {} when there is none. The check started at the top of
    the call; this waits for what is left of
    its budget (`code_bisect_dupcheck_timeout_s` from `started`). Past it the
    bisect answers with the fusion alone and says so (`status: timeout`); any
    error answers as it would without the check."""
    if task is None:
        return {}
    budget = float(settings.code_bisect_dupcheck_timeout_s)
    left = budget - (time.monotonic() - started) if started is not None else budget
    try:
        return dict(await asyncio.wait_for(task, timeout=max(0.05, _budget(left))))
    except TimeoutError:
        if _expired("dupcheck"):
            return {"status": "deadline", "budget_s": budget}
        return {"status": "timeout", "budget_s": budget}
    except Exception as e:  # noqa: BLE001 — fail open
        logger.info("bisect: duplicate-check fix skipped: %s", e)
        return {}


async def _resolve_fix(
    input: Any,
    facts: IssueFacts,
    widening: Widening,
    source: Any,
    org: str,
    tracker_ns: str,
) -> tuple[int, Widening]:
    """The fix a post-hoc bisect reads (the caller's `fix_pr`, else the record's own fix from
    `_record_fix`: the non-backport PR of the issue's `fixes` edges) and the scope widened
    with its files. 0 when there is none."""
    fix_pr = int(input.fix_pr or 0)
    if not fix_pr and facts.fixes and source is not None and facts.qa_ok:
        rec = widening.fix if (widening.fix or {}).get("source") == "record" else None
        rec = rec or await _record_fix(source, org, tracker_ns, facts.fixes)
        fix_pr = int((rec or {}).get("pr_number") or 0)
    if fix_pr and source is not None and facts.qa_ok:
        widening = await _widen_with_fix(source, org, tracker_ns, widening, fix_pr)
    return fix_pr, widening


async def _after_scan(
    repo: CodeRepo,
    source: Any,
    org: str,
    ns: str,
    tracker_ns: str,
    pool: list[bi.Candidate],
    fix_pr: int,
    facts: IssueFacts,
    *,
    named: bool,
    paths: list[str],
    frames: list[str],
    patterns: list[str],
    causes: bool = True,
) -> tuple[list[bi.Candidate], dict[str, Any]]:
    """The priors after the scan: later PRs naming a pool candidate as the cause, then, when
    a fix is known, the post-hoc stages: the fix's lines blamed at its parent, then the fix's
    own statement of the cause. None of them when the graph named the culprit."""
    if named:
        return pool, {}
    if causes and source is not None and facts.qa_ok and pool:
        pool = await _cause_stage(
            source, org, tracker_ns, pool, paths=paths, frames=frames, patterns=patterns
        )
    if not fix_pr:
        return pool, {}
    pool, summary = await _fix_blame_stage(
        repo, org, ns, pool, fix_pr, good=facts.good, bad=facts.bad
    )
    if source is not None and facts.qa_ok:
        pool = await _fix_named_stage(source, org, tracker_ns, pool, fix_pr, summary)
    return pool, summary


async def _widen_with_fix(
    source: Any, org: str, tracker_ns: str, widening: Widening, fix_pr: int
) -> Widening:
    """A known fix's source files join the scan's scope, ahead of the report's: on the
    benchmark 49 of 78 culprits shared a source file with their fix and none with the
    report's trace in 15. One keyed tracker read (`_candidate_paths`); an outage keeps the
    scope."""
    have = widening.fix or {}
    if widening.paths and int(have.get("pr_number") or 0) == int(fix_pr):
        return widening  # the record's fix already widened the scope
    cand: dict[str, Any] = {"pr_number": int(fix_pr)}
    paths = await _candidate_paths(source, org, tracker_ns, cand)
    if not paths:
        return widening
    widening.paths = list(dict.fromkeys([*paths, *widening.paths]))
    widening.fix = widening.fix or cand
    widening.widened_by = [*widening.widened_by, "fix_pr"]
    return widening


async def _fix_named_stage(
    source: Any,
    org: str,
    tracker_ns: str,
    pool: list[bi.Candidate],
    fix_pr: int,
    summary: dict[str, Any],
) -> list[bi.Candidate]:
    """The fix's own description naming its cause ("Caused by #N") is the maintainer's
    bisect; that PR goes first even when the scan never reached it (the later-PR cause stage
    only reorders the pool). One keyed read of the fix's change; fails open."""
    read = getattr(source, "causes_named_by", None)
    if read is None:
        return pool
    try:
        named = await read(org, tracker_ns, int(fix_pr))
    except Exception as e:  # noqa: BLE001 — fails open to the scan's order
        logger.info("bisect: fix #%s cause statement unavailable (%s)", fix_pr, e)
        return pool
    if not named:
        return pool
    summary["named_by_fix"] = named[:3]
    by_number = {c.pr_number: c for c in pool}
    missing = [n for n in named[:3] if n not in by_number]
    rows: dict[int, dict[str, Any]] = {}
    if missing:
        try:
            rows = await source.items_by_number(org, tracker_ns, missing)
        except Exception as e:  # noqa: BLE001 — fails open to the scan's order
            logger.info("bisect: named cause rows unavailable (%s)", e)
    lead: list[bi.Candidate] = []
    for n in named[:3]:
        c = by_number.get(n)
        if c is None:
            r = rows.get(n) or {}
            c = bi.Candidate(
                pr_number=n,
                title=str(r.get("title") or ""),
                author=str(r.get("author") or ""),
                merged_at=str(r.get("merged_at") or r.get("created_at") or ""),
                minor_version=str(r.get("minor_version") or ""),
                url=str(r.get("url") or ""),
            )
        entry = {"pr_number": int(fix_pr), "role": "fix"}
        if entry not in c.named_cause_by:
            c.named_cause_by.append(entry)
        lead.append(c)
    led = {c.pr_number for c in lead}
    return lead + [c for c in pool if c.pr_number not in led]


async def _fix_blame_stage(
    repo: CodeRepo,
    org: str,
    ns: str,
    pool: list[bi.Candidate],
    fix_pr: int,
    *,
    good: str = "",
    bad: str = "",
) -> tuple[list[bi.Candidate], dict[str, Any]]:
    """Fix-blame: the fix's rewritten source lines, blamed at the fix's parent through
    `code.blame_range`, name the PR that wrote them; those PRs go first with `blamed_by_fix`.
    Bounded (a few ranges, a wall budget) and fail-open: the pool as it was, and a summary
    saying why."""
    from server.code.app.blame import handle_code_blame_range, history_git_dir, repo_siblings
    from server.code.app.index import gitrepo
    from server.code.domain.index import fix_blame as fb

    summary: dict[str, Any] = {"fix_pr": fix_pr}
    if _short(STAGE_MIN_S, "fix_blame"):
        summary["skipped"] = "deadline"
        return pool, summary
    try:
        merge = await repo.merge_commit_of(org, ns, fix_pr)
        if not merge:
            summary["skipped"] = "fix_not_in_ledger"
            return pool, summary
        cfg, _ = repo_siblings(org, ns)
        git_dir, gap = await history_git_dir(cfg, "code.bisect_candidates")
        if gap:
            summary["skipped"] = gap
            return pool, summary
        parent = (await gitrepo.run(git_dir, "rev-parse", f"{merge}^1")).strip()
        diff = await gitrepo.run(git_dir, "diff", "-U0", "--no-color", parent, merge)
        ranges = fb.pre_image_ranges(diff)
        summary.update(parent=parent[:12], ranges=len(ranges))
        if not ranges:
            summary["skipped"] = "no_source_lines"
            return pool, summary
        gate = asyncio.Semaphore(FIX_BLAME_CONCURRENCY)

        async def one(r: Any) -> tuple[Any, list[dict[str, Any]]]:
            async with gate:
                out = await handle_code_blame_range(
                    CodeBlameRangeInput.model_validate(
                        {
                            "org": org,
                            "namespace": ns,
                            "path": r.path,
                            "start_line": r.start,
                            "end_line": r.end,
                            "to_sha": parent,
                            "stage": intro.STAGE_LAST_TOUCHED,
                        }
                    )
                )
            return r, [c for c in out.model_dump().get("candidates") or [] if isinstance(c, dict)]

        with code_span("bisect.fix_blame", ranges=len(ranges)):
            per_range = await asyncio.wait_for(
                asyncio.gather(*(one(r) for r in ranges)),
                timeout=_budget(settings.code_bisect_fix_blame_budget_s),
            )
    except TimeoutError:
        summary["skipped"] = "deadline" if _expired("fix_blame") else "timeout"
        return pool, summary
    except Exception as e:  # noqa: BLE001 — a lever beside the scan, never the answer
        logger.info("bisect: fix blame of #%s unavailable (%s)", fix_pr, e)
        summary["skipped"] = "unavailable"
        return pool, summary
    votes = fb.tally(list(per_range), exclude={fix_pr})
    scores = {c.pr_number: c.score for c in pool}
    votes = fb.rank_votes(votes, pool_scores=scores, good=good, bad=bad)
    summary["blamed"] = [
        {"pr_number": v.pr_number, "lines": round(v.lines, 1), "ranges": v.ranges}
        for v in votes[:5]
    ]
    return bi.promote_fix_blame(pool, votes, fix_pr=fix_pr), summary


async def _record_fix(
    source: Any, org: str, tracker_ns: str, fixes: list[int]
) -> dict[str, Any] | None:
    """The record's own fix: the first PR a `fixes` edge names that is merged on trunk, as a
    fix item with `source: record`; None when the edges name only backports, open PRs or
    nothing."""
    from server.tracker.domain.errors import TrackerUnavailable

    for n in fixes[:3]:
        try:
            change = await source.pr_change(org, tracker_ns, n)
        except TrackerUnavailable as e:
            logger.info("bisect: record fix unreadable (%s)", e)
            return None
        if not change or not int(change.get("is_merged") or 0):
            continue
        if int(change.get("is_backport_pr") or 0) or int(change.get("is_cherrypick_pr") or 0):
            continue
        return {
            "pr_number": int(change["pr_number"]),
            "title": str(change.get("title") or "")[:160],
            "url": str(change.get("url") or ""),
            "minor_version": str(change.get("minor_version") or ""),
            "source": "record",
            "epistemic": "record",
            "files": list(change.get("files") or []),
        }
    return None


async def _candidate_paths(
    source: Any, org: str, tracker_ns: str, cand: dict[str, Any]
) -> list[str]:
    """When no frame resolved to a file, the probable fix's own source files
    are where the bug lives: one keyed tracker read of the candidate PR's change (none when
    the record fix already carries its files), disclosed on the candidate as `paths_used`."""
    from server.tracker.domain import backport as bp
    from server.tracker.domain.errors import TrackerUnavailable

    files = cand.pop("files", None)
    if files is None:
        try:
            change = await source.pr_change(org, tracker_ns, int(cand["pr_number"]))
        except TrackerUnavailable as e:
            logger.info("bisect: fix candidate change unavailable (%s)", e)
            return []
        files = list((change or {}).get("files") or [])
    paths = bp.source_files(files)[:_MAX_PATHS]
    cand["paths_used"] = list(paths)
    return paths
