"""Nightly set-wide backport report: for every merged trunk
PR of the last `LOOM_CODE_BACKPORT_GAPS_DAYS` that the maintainers labelled
for backport (`pr-must-backport*` on loom's ledger, QA's `backport_required`),
run `code.backport_gap` with `exact` and `history` in-process and write one
row per release branch into loom.code_backport_gaps, so `code.backport_gaps`
answers "which fixes does 26.6 still owe" from loom's own table.

Paced under the QA budgets: one fix per `pace_s` (three tracker reads each, all
through the source layer — predicate, budget, breaker, cache), at most
`prs_per_pass` fixes a night, never computed first and then the stalest,
and a fix whose every branch row is settled (covered / not_affected exact)
is skipped until its window closes. The leg stops cleanly at `budget_s`
and says where; a QA miss on one fix writes nothing for it (a night of
`unknown` rows would overwrite a day of verdicts) and the next fix runs.
Rows are inserted in batches, never one fix at a time. Restricted trunks
are skipped: the table would hold the private record's titles.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.code.store.backport_gaps import FixStamp, GapRow, build_gap_row
from server.core.datetimes import naive_utc_now_seconds as _now
from server.settings import settings
from server.workers.pass_context import note_failure
from server.workers.status import log_pass_errors, record_pass

logger = logging.getLogger("loom.workers.code_backport_gaps")

LABEL_PREFIX = "pr-must-backport"
VERSION_LABEL_RE = re.compile(r"^v(\d{2}\.\d{1,2})-must-backport")
INSERT_EVERY = 50  # fixes per insert batch (never one-row parts)
CONSUMER = "worker.code_backport_gaps"
SETTLED = frozenset({"covered", "not_affected"})


@dataclass
class BackportGapsResult:
    namespaces: int = 0
    fixes_seen: int = 0
    fixes_computed: int = 0
    fixes_settled: int = 0
    fixes_deferred: int = 0  # past the per-pass cap or the budget: next night
    fixes_unavailable: int = 0
    rows_written: int = 0
    rows_gap: int = 0
    rows_history: int = 0  # decided by the history leg
    rows_undecided: int = 0
    statements: int = 0
    wall_ms: int = 0
    partial: bool = False
    resume: str = ""
    errors: list[str] = field(default_factory=list)

    def outcome(self) -> dict[str, Any]:
        return {
            "namespaces": self.namespaces,
            "fixes_seen": self.fixes_seen,
            "fixes_computed": self.fixes_computed,
            "fixes_settled": self.fixes_settled,
            "fixes_deferred": self.fixes_deferred,
            "fixes_unavailable": self.fixes_unavailable,
            "rows_written": self.rows_written,
            "rows_gap": self.rows_gap,
            "rows_history": self.rows_history,
            "rows_undecided": self.rows_undecided,
            "statements": self.statements,
            "wall_ms": self.wall_ms,
            "partial": self.partial,
            "resume": self.resume,
            "errors": len(self.errors),
        }


async def run_pass() -> BackportGapsResult:
    result = BackportGapsResult()
    with record_pass("code_backport_gaps") as rec:
        await _run(result)
        log_pass_errors(logger, "code_backport_gaps", result.errors)
        rec.outcome = result.outcome()
    return result


def trunks() -> list[tuple[Any, list[Any]]]:
    """[(trunk config, its release-branch configs)] — the standard trunks
    (`master` / `main`) that have at least one release branch configured."""
    from server.code.domain.index.repos import TRUNK_BRANCHES, configured_repos

    repos = configured_repos()
    out: list[tuple[Any, list[Any]]] = []
    for cfg in repos:
        if cfg.git_branch not in TRUNK_BRANCHES or cfg.classification != "standard":
            continue
        sibs = [
            r
            for r in repos
            if r.repo == cfg.repo
            and r.org == cfg.org
            and r.namespace != cfg.namespace
            and r.classification == "standard"
        ]
        if sibs:
            out.append((cfg, sibs))
    return out


async def _run(
    result: BackportGapsResult,
    *,
    now: datetime | None = None,
    repo: Any = None,
    code_repo: Any = None,
    source_factory: Any = None,
    runner: Any = None,
) -> None:
    from server.code.app.backport import run_backport_gap
    from server.code.app.index.bootstrap import awaits_first_index
    from server.code.store.backport_gaps import CodeBackportGapsRepo
    from server.code.store.index import CodeRepo

    started = time.monotonic()
    repo = repo or CodeBackportGapsRepo()
    code_repo = code_repo or CodeRepo()
    run = runner or run_backport_gap
    for cfg, sibs in trunks():
        try:
            if await awaits_first_index(cfg, code_repo):
                continue
            await refresh_namespace(
                cfg,
                sibs,
                repo,
                code_repo,
                result=result,
                now=now or _now(),
                runner=run,
                source_factory=source_factory,
                started=started,
            )
            result.namespaces += 1
        except Exception as e:
            result.errors.append(f"{cfg.namespace}: {type(e).__name__}: {e}")
            note_failure(e, f"code_backport_gaps:{cfg.namespace}")
            logger.exception("code_backport_gaps: namespace %s failed", cfg.namespace)
    result.wall_ms = int((time.monotonic() - started) * 1000)


async def _wanted_from_tracker(
    cfg: Any,
    since: datetime,
    source_factory: Any,
    result: BackportGapsResult,
) -> set[int]:
    """The PR numbers QA marks `backport_required` in the window — one bounded
    statement through the source layer; a miss is noted and the ledger's
    labels stand alone."""
    from server.tracker.app.binding import authorized_tracker_namespace
    from server.tracker.app.source import tracker_source
    from server.tracker.domain.errors import TrackerUnavailable

    tracker_ns = await authorized_tracker_namespace(cfg.org, cfg.namespace)
    if not tracker_ns:
        return set()
    src = (source_factory or tracker_source)(cfg.org, tracker_ns)
    try:
        nums = await src.backport_wanted_prs(cfg.org, tracker_ns, since=since)
        result.statements += 1
        return {int(n) for n in nums}
    except TrackerUnavailable as e:
        result.errors.append(f"{cfg.namespace}: tracker backport_required unavailable: {e}")
        return set()


def order_fixes(
    fixes: dict[int, dict[str, Any]],
    stamps: dict[int, FixStamp],
    *,
    branch_count: int,
) -> tuple[list[int], int]:
    """(the fixes to compute, never computed first — newest merge first — then
    the stalest; the settled fixes skipped). A fix is settled when every
    branch has a row and every row is settled."""
    todo: list[tuple[int, datetime, int]] = []
    settled = 0
    for pr, f in fixes.items():
        st = stamps.get(pr)
        if st and st.settled and st.rows >= branch_count:
            settled += 1
            continue
        latest = st.latest if st else None
        merged = f.get("merged_at") or datetime(1970, 1, 1)
        # never computed sorts first (epoch stamp), then by staleness; within a
        # tier the newest merge first
        todo.append((pr, latest or datetime(1970, 1, 1), -int(merged.timestamp())))
    todo.sort(key=lambda t: (t[1], t[2]))
    return [t[0] for t in todo], settled


async def collect_fixes(
    cfg: Any,
    code_repo: Any,
    *,
    since: datetime,
    result: BackportGapsResult,
    source_factory: Any = None,
) -> dict[int, dict[str, Any]]:
    """pr_number → {labels, title, merged_at, source} for the trunk's labelled
    fixes of the window: the ledger's must-backport labels (one statement)
    and QA's `backport_required` (one bounded statement), merged."""
    ledger = await code_repo.labelled_fixes_in_window(
        cfg.org,
        cfg.namespace,
        since=since,
        label_prefix=LABEL_PREFIX,
    )
    result.statements += 1
    fixes: dict[int, dict[str, Any]] = {}
    for r in ledger:
        pr = int(r.get("pr_number") or 0)
        if pr <= 0:
            continue
        fixes[pr] = {
            "labels": [str(x) for x in (r.get("labels") or [])],
            "title": str(r.get("title") or ""),
            "merged_at": r.get("merged_at"),
            "source": "label",
        }
    for pr in await _wanted_from_tracker(cfg, since, source_factory, result):
        if pr in fixes:
            fixes[pr]["source"] = "label+tracker"
        else:
            fixes[pr] = {"labels": [], "title": "", "merged_at": None, "source": "tracker"}
    return fixes


async def compute_fix(
    cfg: Any,
    pr: int,
    fix: dict[str, Any],
    *,
    runner: Any,
    result: BackportGapsResult,
    stamp: datetime,
    version: int,
) -> list[GapRow]:
    """One fix's composed read → its table rows; [] when the fix could not be
    read or its tracker record did not answer (a night of unknowns must not
    overwrite verdicts), counted on the result either way."""
    from server.code.app.backport import BackportGapRequest

    try:
        out = await runner(
            BackportGapRequest(
                org=cfg.org,
                namespace=cfg.namespace,
                pr_number=pr,
                exact=True,
                history=True,
                consumer=CONSUMER,
                trace=False,
            )
        )
    except Exception as e:  # noqa: BLE001 — one fix must not end the pass
        result.fixes_unavailable += 1
        note_failure(e, f"code_backport_gaps:#{pr}")
        logger.info("code_backport_gaps: #%s failed: %s", pr, e)
        return []
    if bool(getattr(out, "tracker_unavailable", False)) or "tracker_unbound" in list(
        out.causes or []
    ):
        result.fixes_unavailable += 1
        return []
    result.fixes_computed += 1
    return table_rows(
        cfg.org, cfg.namespace, pr, fix, out, stamp=stamp, version=version, result=result
    )


async def refresh_namespace(
    cfg: Any,
    sibs: list[Any],
    repo: Any,
    code_repo: Any,
    *,
    result: BackportGapsResult,
    now: datetime,
    runner: Any,
    source_factory: Any = None,
    started: float | None = None,
) -> None:
    """One trunk's pass: the labelled fixes of the window (ledger + QA), the
    table's stamps, then the composed read per fix — paced, capped, budgeted
    — written in batches with one computed_at / version per pass."""
    started = time.monotonic() if started is None else started
    days = int(settings.code_backport_gaps_days)
    cap = int(settings.code_backport_gaps_prs_per_pass)
    pace = float(settings.code_backport_gaps_pace_s)
    budget = float(settings.code_backport_gaps_budget_s)
    since = now - timedelta(days=max(1, days))
    fixes = await collect_fixes(
        cfg,
        code_repo,
        since=since,
        result=result,
        source_factory=source_factory,
    )
    result.fixes_seen += len(fixes)
    stamps = await repo.fix_stamps(cfg.org, cfg.namespace)
    result.statements += 1
    todo, settled = order_fixes(fixes, stamps, branch_count=len(sibs))
    result.fixes_settled += settled
    stamp, version = now, int(time.time_ns())
    batch: list[GapRow] = []
    for i, pr in enumerate(todo):
        if i >= cap or (time.monotonic() - started) > budget:
            result.fixes_deferred += len(todo) - i
            result.partial = True
            result.resume = f"{cfg.namespace}:#{pr}"
            break
        t0 = time.monotonic()
        batch.extend(
            await compute_fix(
                cfg,
                pr,
                fixes[pr],
                runner=runner,
                result=result,
                stamp=stamp,
                version=version,
            )
        )
        if len(batch) >= INSERT_EVERY * max(1, len(sibs)):
            await repo.insert_rows(batch)
            result.statements += 1
            result.rows_written += len(batch)
            batch = []
        wait = pace - (time.monotonic() - t0)
        if wait > 0:
            await asyncio.sleep(wait)
    if batch:
        await repo.insert_rows(batch)
        result.statements += 1
        result.rows_written += len(batch)


def table_rows(
    org: str,
    code_ns: str,
    pr: int,
    fix: dict[str, Any],
    out: Any,
    *,
    stamp: datetime,
    version: int,
    result: BackportGapsResult | None = None,
) -> list[GapRow]:
    """One fix's `code.backport_gap` answer → one table row per branch."""
    labels = list(fix.get("labels") or [])
    wanted_versions = {m.group(1) for lab in labels if (m := VERSION_LABEL_RE.match(lab))}
    fix_block = dict(getattr(out, "fix", {}) or {})
    merged = fix.get("merged_at")
    if merged is None and fix_block.get("merged_at"):
        try:
            merged = datetime.fromisoformat(str(fix_block["merged_at"])).replace(tzinfo=None)
        except ValueError:
            merged = None
    rows: list[GapRow] = []
    for b in list(getattr(out, "branches", []) or []):
        verdict = str(b.get("verdict") or "unknown")
        epistemic = str(b.get("epistemic") or "")
        basis = str(b.get("basis") or "")
        pick = dict(b.get("cherry_pick") or {})
        patch = dict((b.get("presence") or {}).get("patch") or {})
        if result is not None:
            result.rows_gap += verdict == "gap"
            result.rows_history += basis == "history"
            result.rows_undecided += (
                verdict in ("gap", "not_affected")
                and epistemic == "lower_bound"
                and basis != "history"
                and patch.get("reason") == "undecided"
            )
        rows.append(
            build_gap_row(
                org=org,
                code_namespace=code_ns,
                branch_namespace=str(b.get("namespace") or ""),
                pr_number=int(pr),
                branch=str(b.get("branch") or ""),
                verdict=verdict,
                epistemic=epistemic,
                basis=basis,
                reason=str(b.get("reason") or ""),
                cherry_pick_pr=int(pick.get("pr_number") or 0),
                cherry_pick_sha=str(pick.get("merge_sha") or ""),
                backport_pr=int((b.get("backport") or {}).get("pr_number") or 0),
                label_wanted=1,
                wanted_branch=int(str(b.get("branch") or "") in wanted_versions),
                wanted_source=str(fix.get("source") or "label"),
                settled=int(verdict in SETTLED and epistemic == "exact"),
                pr_title=(str(fix.get("title") or fix_block.get("title") or ""))[:300],
                pr_merged_at=merged,
                pr_minor_version=str(fix_block.get("minor_version") or ""),
                computed_at=stamp,
                version=version,
            )
        )
    return rows


__all__ = [
    "CONSUMER",
    "LABEL_PREFIX",
    "VERSION_LABEL_RE",
    "BackportGapsResult",
    "collect_fixes",
    "compute_fix",
    "order_fixes",
    "refresh_namespace",
    "run_pass",
    "table_rows",
    "trunks",
]
