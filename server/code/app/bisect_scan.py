"""The bisect scan: which merged PRs to read for an issue (the plan), and the ranked
candidate rows the linked, path and history legs return."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.code.app.bisect_facts import _MAX_PATHS, IssueFacts, Widening
from server.code.app.spans import code_span
from server.core.datetimes import naive_utc_now_seconds
from server.settings import settings
from server.tracker.domain import bisect as bi

logger = logging.getLogger(__name__)


def _dirs_of(paths: list[str]) -> list[str]:
    """The directories of the resolved source files (`src/Storages/MergeTree/`),
    deduplicated, at most eight — the widened candidate scope."""
    out: list[str] = []
    for p in paths:
        d = p.rsplit("/", 1)[0] + "/" if "/" in p else ""
        if d and d not in out:
            out.append(d)
    return out[:8]


@dataclass
class ScanPlan:
    """Everything the QA scan and the answer need to know about its scope."""

    paths: list[str] = field(default_factory=list)
    hub_dropped: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    scope_patterns: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    before: datetime | None = None
    after: datetime | None = None

    @property
    def timed(self) -> bool:
        """Bounded by merge time instead of releases."""
        return not self.versions and self.before is not None

    @property
    def widened(self) -> bool:
        return bool(self.dirs or self.scope_patterns)

    @property
    def scope(self) -> str:
        if self.dirs:
            return "directory"
        return "patterns" if self.scope_patterns else "file"


def _plan(
    input: Any,
    *,
    resolved: list[dict[str, Any]],
    widening: Widening,
    frames: list[str],
    patterns: list[str],
    facts: IssueFacts,
) -> ScanPlan:
    """The scan's scope from the frames' files, the caller's paths and the
    frameless widening: hub files leave the filter, the frames' directories join when frames
    or patterns can rank inside them, the component's directories and the rare patterns when
    nothing else bounded the scan."""
    paths = list(
        dict.fromkeys(
            [
                # a known fix's files lead: they are where the bug lived
                *(widening.paths if "fix_pr" in widening.widened_by else []),
                *(str(p) for p in input.paths if p),
                *(p for r in resolved for p in r["paths"]),
                *widening.paths,
            ]
        )
    )[:_MAX_PATHS]
    paths, hub_dropped = bi.drop_hub_paths(paths)
    dirs = _dirs_of(paths) if (input.widen_to_directories and (frames or patterns)) else []
    dirs = list(dict.fromkeys([*dirs, *widening.dirs]))[:16]
    versions = bi.versions_between(
        facts.good,
        facts.bad,
        master=facts.master,
        lookback=int(settings.code_bisect_lookback_minors),
    )
    return ScanPlan(
        paths=paths,
        hub_dropped=hub_dropped,
        dirs=dirs,
        scope_patterns=widening.scope_patterns,
        versions=versions,
        before=None if versions else facts.before,
        after=None if versions else facts.after,
    )


async def _scan(
    source: Any,
    org: str,
    tracker_ns: str,
    plan: ScanPlan,
    *,
    frames: list[str],
    patterns: list[str],
    weights: dict[str, float],
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """The QA diff scan over the plan's scope; (rows, qa ok)."""
    from server.tracker.domain.errors import TrackerUnavailable

    with code_span("bisect.tracker_prs", seeds=len(plan.paths) or len(plan.scope_patterns)):
        try:
            rows = await source.merged_prs_touching(
                org,
                tracker_ns,
                plan.paths,
                plan.versions,
                symbols=frames,
                patterns=patterns,
                dirs=plan.dirs,
                scope_patterns=plan.scope_patterns,
                pattern_weights=weights,
                limit=limit * 3,
                before=plan.before,
                after=plan.after,
            )
        except TrackerUnavailable as e:
            logger.info("bisect: qa unavailable (%s)", e)
            return [], False
    return list(rows), True


async def _candidate_rows(
    source: Any,
    org: str,
    ns: str,
    tracker_ns: str,
    plan: ScanPlan,
    facts: Any,
    *,
    scan: bool,
    frames: list[str],
    patterns: list[str],
    weights: dict[str, float],
    limit: int,
) -> tuple[list[dict[str, Any]], bool, str]:
    """(rows, qa ok, where the candidates came from). The tracker's diff scan when the
    repository has one that answers; else loom's own PR history of the repository (merged
    PRs that touched the suspect paths in the window), so bisect works for a repository with
    no tracker, or one whose tracker holds no changed files."""
    rows: list[dict[str, Any]] = []
    qa_ok = facts.qa_ok
    if source is not None and qa_ok and scan:
        rows, qa_ok = await _scan(
            source,
            org,
            tracker_ns,
            plan,
            frames=frames,
            patterns=patterns,
            weights=weights,
            limit=limit,
        )
        rows += await _linked_scan(
            org,
            ns,
            tracker_ns,
            plan,
            {int(r.get("pr_number") or 0) for r in rows},
            frames=frames,
            patterns=patterns,
            weights=weights,
            limit=limit,
        )
    if rows or not scan or not (plan.paths or plan.dirs):
        return rows, qa_ok, "tracker" if rows else "none"
    history = await _code_history_rows(org, ns, plan, limit=limit * 3)
    if history:
        return history, True, "code_history"
    return rows, qa_ok, "none"


async def _linked_scan(
    org: str,
    ns: str,
    primary: str,
    plan: ScanPlan,
    seen: set[int],
    *,
    frames: list[str],
    patterns: list[str],
    weights: dict[str, float],
    limit: int,
) -> list[dict[str, Any]]:
    """The same diff scan over the repository's other linked trackers. A tracker that holds
    this repository's pull requests numbers them as the repository does, so a PR the primary
    already returned is kept once (the primary's row); a tracker that maps no pull requests
    answers nothing (its table is an empty stub). Each row names its tracker; one that fails
    is left out and the primary's answer stands."""
    from server.tracker.app.binding import authorized_tracker_namespaces
    from server.tracker.app.source import tracker_source

    extra = [t for t in await authorized_tracker_namespaces(org, ns) if t != primary]
    if not extra:
        return []
    found = await asyncio.gather(
        *[
            _scan(
                tracker_source(org, t),
                org,
                t,
                plan,
                frames=frames,
                patterns=patterns,
                weights=weights,
                limit=limit,
            )
            for t in extra
        ],
        return_exceptions=True,
    )
    out: list[dict[str, Any]] = []
    for t, got in zip(extra, found, strict=True):
        if isinstance(got, BaseException):
            logger.info("bisect: linked tracker %s failed: %s", t, got)
            continue
        for r in got[0]:
            n = int(r.get("pr_number") or 0)
            if n and n not in seen:
                seen.add(n)
                out.append({**r, "tracker_namespace": t})
    return out


async def _code_history_rows(
    org: str, ns: str, plan: ScanPlan, *, limit: int
) -> list[dict[str, Any]]:
    """Merged PRs of the repository that touched the plan's paths (or its directories)
    inside the window, from `loom.code_pr_history`, the ledger loom keeps for every indexed
    repository, tracker or not. The window is the plan's (`before` / `after`), or the last
    `code_bisect_history_days` before now."""
    from server.code.domain.index.repos import repo_for_namespace
    from server.code.store.index import CodeRepo

    now = naive_utc_now_seconds()
    before = plan.before or now
    days = max(1, int(settings.code_bisect_history_days))
    after = plan.after or (before - timedelta(days=days))
    paths = list(plan.paths)[:200]
    dirs = [d.rstrip("/") + "/" for d in plan.dirs][:20]
    try:
        with code_span("bisect.code_history", seeds=len(paths) or len(dirs)):
            found = await CodeRepo().prs_touching_in_window(
                org, ns, paths=paths, dirs=dirs, before=before, after=after, limit=limit
            )
    except Exception as e:  # noqa: BLE001 — a failed history read is no candidates
        logger.info("bisect: code history of %s/%s unavailable: %s", org, ns, e)
        return []
    cfg = repo_for_namespace(org, ns)
    repo = cfg.repo if cfg else ""
    wanted = set(paths)
    out: list[dict[str, Any]] = []
    for r in found:
        files = [str(f) for f in (r.get("files") or [])]
        matched = [f for f in files if f in wanted or any(f.startswith(d) for d in dirs)]
        n = int(r.get("pr_number") or 0)
        out.append(
            {
                "pr_number": n,
                "title": str(r.get("title") or ""),
                "author": str(r.get("author") or ""),
                "merged_at": r.get("merged_at"),
                "matched_paths": matched,
                "affected_file_paths": files,
                "url": f"https://github.com/{repo}/pull/{n}" if repo and n else "",
                "additions": int(r.get("additions") or 0),
                "deletions": int(r.get("deletions") or 0),
            }
        )
    return out
