"""Daily issue–PR link recovery: for every issue opened in
the last `LOOM_TRACKER_LINKS_ISSUE_DAYS` of each configured tracker namespace, rank
the merged trunk PRs of the window after it by BM25F over title and body
(the QA text index admits the candidates), the issue's stack frames in the
PR's diff and file list, the closing mention and the component, and write
the top `LOOM_TRACKER_LINKS_PER_ISSUE` into loom.qa_link_candidates.

Reads the tracker record only through the source layer (predicate, budget,
breaker, cache); one candidate statement per
issue plus one keyed diff read for the issues that carry frames; nothing
through the play proxy's rationed views. An issue whose window closed
before its last pass is final and skipped; a QA miss on one issue is noted
on the pass and the next issue runs. Restricted namespaces are skipped: the
table would copy the private record's titles into loom; they answer
`not_computed`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.core.datetimes import naive_utc_now_seconds as _now
from server.llm import customer
from server.settings import settings
from server.tracker.app import links as lk
from server.tracker.domain import similar as sim
from server.workers.first_pass import OwnedTable
from server.workers.pass_context import note_failure, set_namespace
from server.workers.status import log_pass_errors, record_pass

logger = logging.getLogger("loom.workers.tracker_links")

CANDIDATES_PER_ISSUE = 50
ISSUE_PAGE = 400

# The table this pass fills; a configured namespace it holds
# no row for makes the first pass due at boot instead of after the boot
# stagger (hours for a daily worker).
OWNED_TABLES = (OwnedTable("loom.qa_link_candidates", namespace_column="qa_namespace"),)


def due_scope() -> list[tuple[str, str]]:
    """(org, namespace) of every configured tracker namespace this pass writes —
    restricted ones are skipped by the pass, so they are not in scope."""
    from server.tracker.domain.namespaces import configured_namespaces

    return sorted(
        (cfg.org, ns) for ns, cfg in configured_namespaces().items() if not cfg.restricted
    )


@dataclass
class TrackerLinksResult:
    namespaces: int = 0
    issues_seen: int = 0
    issues_scored: int = 0
    issues_empty: int = 0
    issues_final: int = 0
    issues_unavailable: int = 0
    skipped_restricted: int = 0
    rows_written: int = 0
    statements: int = 0
    wall_ms: int = 0
    # The weekly duplicate-fit leg
    dupcheck_fits: int = 0
    dupcheck_fit_n: int = 0
    dupcheck_fit_auc: float = 0.0
    dupcheck_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def outcome(self) -> dict[str, Any]:
        # The row's counters first, under their own names —
        # issues seen / handled (scored, empty or final) / candidate rows.
        handled = self.issues_scored + self.issues_empty + self.issues_final
        return {
            "items_planned": self.issues_seen,
            "items_done": handled,
            "rows_written": self.rows_written,
            "namespaces": self.namespaces,
            "issues_seen": self.issues_seen,
            "issues_scored": self.issues_scored,
            "issues_empty": self.issues_empty,
            "issues_final": self.issues_final,
            "issues_unavailable": self.issues_unavailable,
            "skipped_restricted": self.skipped_restricted,
            "statements": self.statements,
            "dupcheck_fits": self.dupcheck_fits,
            "dupcheck_fit_n": self.dupcheck_fit_n,
            "dupcheck_fit_auc": self.dupcheck_fit_auc,
            "dupcheck_skipped": self.dupcheck_skipped,
            "wall_ms": self.wall_ms,
            "errors": len(self.errors),
        }


async def run_pass() -> TrackerLinksResult:
    result = TrackerLinksResult()
    with record_pass("tracker_links") as rec:
        await _run(result)
        log_pass_errors(logger, "tracker_links", result.errors)
        rec.outcome = result.outcome()
    return result


async def _run(
    result: TrackerLinksResult,
    *,
    now: datetime | None = None,
    repo: Any = None,
    source_factory: Any = None,
) -> None:
    from server.tracker.app.source import tracker_source
    from server.tracker.domain.errors import TrackerUnavailable
    from server.tracker.domain.namespaces import configured_namespaces
    from server.tracker.store.links import TrackerLinksRepo

    started = time.monotonic()
    repo = repo or TrackerLinksRepo()
    make = source_factory or tracker_source
    for ns, cfg in sorted(configured_namespaces().items()):
        if cfg.restricted:
            # the table would hold the private record's titles in loom; a
            # restricted namespace answers `not_computed` until a title-free
            # variant exists
            result.skipped_restricted += 1
            continue
        if await customer.switched_off(cfg.org):
            continue  # scoring embeds: the namespace is scored when the org is back on
        set_namespace(ns)  # Where a cut pass stopped
        src = make(cfg.org, ns)
        try:
            # The refit replays the gold set through the vector leg: that org's embeds.
            with customer.for_org(cfg.org):
                await score_namespace(src, repo, cfg.org, ns, result=result, now=now or _now())
                result.namespaces += 1
                await refit_dupcheck(src, cfg.org, ns, result=result, now=now or _now())
        except TrackerUnavailable as e:
            result.errors.append(f"{ns}: tracker_unavailable: {e}")
            note_failure(e, f"tracker_links:{ns}")
        except customer.HOLDS:
            continue  # a held call is no failure: the namespace is scored once the org is back
        except Exception as e:
            result.errors.append(f"{ns}: {type(e).__name__}: {e}")
            note_failure(e, f"tracker_links:{ns}")
            logger.exception("tracker_links: namespace %s failed", ns)
    result.wall_ms = int((time.monotonic() - started) * 1000)


async def refit_dupcheck(
    src: Any,
    org: str,
    ns: str,
    *,
    result: TrackerLinksResult,
    now: datetime,
    state_repo: Any = None,
) -> None:
    """The weekly refit of tracker.duplicate_check's logistic fit
    for this namespace — due when the stored fit is `tracker_dupcheck_refit_interval_s`
    old; the pass counts fits, the rows fitted, the in-sample AUC and the
    namespaces skipped (fresh, restricted, no gold, off). Never raises."""
    from server.tracker.app import dupcheck_fit as df

    if not hasattr(src, "duplicate_gold"):
        result.dupcheck_skipped += 1  # a source without the gold readers has nothing to fit
        return
    out = await df.refit_namespace(src, state_repo or df.state_repo(), org, ns, now=now)
    if out.status == "written":
        result.dupcheck_fits += 1
        result.dupcheck_fit_n += out.n
        result.dupcheck_fit_auc = out.auc
    else:
        result.dupcheck_skipped += 1
    if out.status == "failed":
        result.errors.append(f"{ns}: dupcheck_fit {out.detail}")
    result.statements += out.replays


def _stats(raw: dict[str, Any]) -> sim.TokenStats:
    return sim.TokenStats(
        n=int(raw.get("n") or 0),
        df=dict(raw.get("df") or {}),
        avg_title_len=float(raw.get("avg_title_len") or 10.0),
        avg_body_len=float(raw.get("avg_body_len") or 900.0),
    )


async def score_namespace(
    src: Any,
    repo: Any,
    org: str,
    ns: str,
    *,
    result: TrackerLinksResult,
    now: datetime,
    weights: lk.Weights | None = None,
) -> None:
    """One namespace's pass: the PR vocabulary once, then the issues of the
    last `issue_days` a page at a time, each scored and written as a list
    stamped with one computed_at / version per pass."""
    w = weights or lk.weights_from_settings()
    issue_days = int(settings.tracker_links_issue_days)
    window_days = int(settings.tracker_links_window_days)
    per_issue = int(settings.tracker_links_per_issue)
    stats = _stats(await src.pr_token_stats(org, ns))
    result.statements += 2
    since = now - timedelta(days=max(1, issue_days))
    stamp, version = now, int(time.time_ns())
    before = 0
    while True:
        issues = await src.link_issues(org, ns, since=since, before_number=before, limit=ISSUE_PAGE)
        result.statements += 1
        if not issues:
            break
        final = await repo.latest_computed(org, ns, [int(i["number"]) for i in issues])
        rows = await _score_page(
            src,
            org,
            ns,
            issues,
            stats,
            final,
            result=result,
            now=now,
            weights=w,
            window_days=window_days,
            per_issue=per_issue,
            stamp=stamp,
            version=version,
        )
        await repo.insert_candidates(rows)
        result.rows_written += len(rows)
        before = int(issues[-1]["number"])
        if len(issues) < ISSUE_PAGE:
            break


async def _score_page(
    src: Any,
    org: str,
    ns: str,
    issues: list[dict[str, Any]],
    stats: sim.TokenStats,
    final: dict[int, datetime],
    *,
    result: TrackerLinksResult,
    now: datetime,
    weights: lk.Weights,
    window_days: int,
    per_issue: int,
    stamp: datetime,
    version: int,
) -> list[dict[str, Any]]:
    from server.tracker.domain.errors import TrackerUnavailable

    rows: list[dict[str, Any]] = []
    for it in issues:
        result.issues_seen += 1
        created = it.get("created_at")
        last = final.get(int(it.get("number") or 0))
        closed = (
            isinstance(created, datetime)
            and last is not None
            and (last > created + timedelta(days=window_days))
        )
        if closed:
            result.issues_final += 1  # the window closed before the last pass: nothing can change
            continue
        try:
            ranked, q, used = await score_issue(
                src,
                org,
                ns,
                it,
                stats,
                now=now,
                weights=weights,
                window_days=window_days,
                per_issue=per_issue,
            )
        except TrackerUnavailable as e:
            result.issues_unavailable += 1
            note_failure(e, f"tracker_links:issue:{it.get('number')}")
            continue
        result.statements += used
        if not ranked:
            result.issues_empty += 1
            continue
        result.issues_scored += 1
        rows.extend(lk.table_rows(org, ns, q, ranked, computed_at=stamp, version=version))
    return rows


async def score_issue(
    src: Any,
    org: str,
    ns: str,
    issue: dict[str, Any],
    stats: sim.TokenStats,
    *,
    now: datetime,
    weights: lk.Weights,
    window_days: int,
    per_issue: int,
    candidates: int = CANDIDATES_PER_ISSUE,
) -> tuple[list[dict[str, Any]], lk.IssueQuery, int]:
    """One issue → (its ranked candidates, its query, statements used): the
    BM25F candidate read over the merge window, then — only when the issue
    carries frames — one keyed diff read of the top rows for the symbol
    overlap. Also the bench's unit: `now` closes the window for a gold issue
    at its own horizon."""
    q = lk.issue_query(
        int(issue.get("number") or 0),
        str(issue.get("title") or ""),
        str(issue.get("body") or ""),
        stats,
        component=str(issue.get("component") or ""),
        created_at=issue.get("created_at"),
    )
    if not q.terms or q.created_at is None or q.number <= 0:
        return [], q, 0
    since, until = lk.window(q.created_at, window_days=window_days, now=now)
    if until <= since:
        return [], q, 0
    rows = await src.link_pr_candidates(
        org,
        ns,
        issue_number=q.number,
        terms=q.terms,
        idf=q.idf,
        avg_title_len=stats.avg_title_len,
        avg_body_len=stats.avg_body_len,
        since=since,
        until=until,
        files=q.files,
        limit=candidates,
    )
    used = 1
    hits: dict[int, int] = {}
    if q.symbols and rows:
        facts = await src.prs_facts(
            org,
            ns,
            [int(r["pr_number"]) for r in rows[: max(per_issue, 20)]],
            names=q.symbols,
        )
        used += 1
        hits = {int(n): len(f.get("matched_symbols") or []) for n, f in facts.items()}
    return lk.rank(q, rows, weights=weights, symbol_hits=hits, per_issue=per_issue), q, used


__all__ = ["TrackerLinksResult", "refit_dupcheck", "run_pass", "score_issue", "score_namespace"]
