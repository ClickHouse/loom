"""code.backport_gaps: the set-wide backport report — which merged fixes labelled for
backport does a release branch still owe?

`code.backport_gap` answers for one fix; this op answers per release. The nightly
`code_backport_gaps` worker runs the composed read with `exact` and `history` for every
labelled fix of the window and writes one row per (fix, branch) into
loom.code_backport_gaps; this handler is a keyed read of that table: one branch's rows (a
range read on the key prefix) or every branch's, gaps first, the fixes a
`v<branch>-must-backport` label names first within a verdict, newest fix first, with counts
per verdict and per branch, the pass's stamps, and `stale` when no pass has written the
trunk or the last one is older than two intervals. Every branch namespace is authorized
first, fail closed (the same read gate the other fan-outs share); rows of a branch the
configuration no longer names are not served.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from server.code.api.models_history import CodeBackportGapsOutput
from server.code.app.backport import resolve_branches
from server.code.app.common import require_code_intel, resolve_code_identity
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.store.backport_gaps import CodeBackportGapsRepo, GapRow
from server.core.datetimes import naive_utc_now
from server.settings import settings
from server.tracker.domain import backport as bp

logger = logging.getLogger(__name__)

_RANK = {v: i for i, v in enumerate(bp.VERDICTS)}


def _iso(v: Any) -> str:
    return v.isoformat() if isinstance(v, datetime) else ""


def wire_row(r: GapRow) -> dict[str, Any]:
    """One table row on the wire: numbers and names of the public record."""
    pick_pr = int(r.get("cherry_pick_pr") or 0)
    return {
        "pr_number": int(r.get("pr_number") or 0),
        "title": str(r.get("pr_title") or ""),
        "merged_at": _iso(r.get("pr_merged_at")),
        "minor_version": str(r.get("pr_minor_version") or ""),
        "branch": str(r.get("branch") or ""),
        "branch_namespace": str(r.get("branch_namespace") or ""),
        "verdict": str(r.get("verdict") or "unknown"),
        "epistemic": str(r.get("epistemic") or ""),
        "basis": str(r.get("basis") or ""),
        "reason": str(r.get("reason") or ""),
        "cherry_pick": (
            {"pr_number": pick_pr, "merge_sha": str(r.get("cherry_pick_sha") or "")}
            if pick_pr
            else {}
        ),
        "backport_pr": int(r.get("backport_pr") or 0),
        "label_wanted": bool(int(r.get("label_wanted") or 0)),
        "wanted_branch": bool(int(r.get("wanted_branch") or 0)),
        "wanted_source": str(r.get("wanted_source") or ""),
        "settled": bool(int(r.get("settled") or 0)),
        "computed_at": _iso(r.get("computed_at")),
    }


def rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gaps first; within a verdict the fixes labelled for THIS branch first;
    then the newest fix; then the newest branch."""
    return sorted(
        rows,
        key=lambda r: (
            _RANK.get(str(r.get("verdict") or ""), 9),
            -int(bool(r.get("wanted_branch"))),
            -int(r.get("pr_number") or 0),
            tuple(-x for x in (bp.version_key(str(r.get("branch") or "")) or (0, 0))),
        ),
    )


def counts_of(rows: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    total: dict[str, int] = {v: 0 for v in bp.VERDICTS}
    by_branch: dict[str, dict[str, int]] = {}
    for r in rows:
        v = str(r.get("verdict") or "unknown")
        total[v] = total.get(v, 0) + 1
        b = by_branch.setdefault(str(r.get("branch") or r.get("branch_namespace") or ""), {})
        b[v] = b.get(v, 0) + 1
    return total, by_branch


def is_stale(last: datetime | None, *, now: datetime) -> bool:
    if last is None:
        return True
    interval = float(settings.code_backport_gaps_interval_seconds)
    return (now - last).total_seconds() > 2.0 * interval


async def handle_code_backport_gaps(input: Any) -> Any:
    from server.code.app import search_fanout as fanout

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    wanted = [str(input.branch).strip()] if str(input.branch or "").strip() else []
    branches = resolve_branches(org, ns, wanted)
    skipped_branches: list[dict[str, str]] = []
    if branches and not wanted:  # an expansion Loom made skips and discloses
        readable, skipped_branches = await fanout.readable_subset(org, [b for b, _ in branches])
        branches = [b for b in branches if b[0] in set(readable)]
    branch_ns = [b for b, _ in branches]
    if branch_ns:
        await fanout.authorize(org, branch_ns, op="code.backport_gaps")
    now = naive_utc_now()
    since = now - timedelta(days=int(input.days))
    repo = CodeBackportGapsRepo()
    with code_span("backport_gaps.read", seeds=len(branch_ns)):
        if wanted and branch_ns:
            raw = await repo.rows_for_branch(
                org,
                ns,
                branch_ns[0],
                since=since,
                wanted_only=bool(input.wanted_only),
                limit=int(input.limit),
            )
        elif branch_ns:
            raw = await repo.rows_for_namespace(
                org,
                ns,
                since=since,
                wanted_only=bool(input.wanted_only),
                limit=int(input.limit),
                branches=branch_ns,
            )
        else:
            raw = []
        stats = await repo.namespace_stats(org, ns)
    allowed = set(branch_ns)
    rows = rank_rows([wire_row(r) for r in raw if str(r.get("branch_namespace") or "") in allowed])
    rows = rows[: int(input.limit)]
    total, by_branch = counts_of(rows)
    set_code_span_attributes(
        route="backport_gaps",
        results=total.get(bp.GAP, 0),
        fanout=len(branch_ns),
        consumer=input.consumer,
    )
    return CodeBackportGapsOutput(
        skipped_branches=skipped_branches,
        branch=branches[0][1] if wanted and branches else "",
        branch_namespace=branch_ns[0] if wanted and branch_ns else "",
        branches=branch_ns,
        rows=rows,
        counts=total,
        counts_by_branch=by_branch,
        fixes=len({r["pr_number"] for r in rows}),
        wanted_only=bool(input.wanted_only),
        since=since.isoformat(),
        computed={
            "fixes": stats.fixes,
            "rows": stats.rows,
            "first_computed": _iso(stats.first_computed),
            "last_computed": _iso(stats.last_computed),
        },
        stale=is_stale(stats.last_computed, now=now),
        ms=int((time.monotonic() - started) * 1000),
    )


__all__ = ["counts_of", "handle_code_backport_gaps", "is_stale", "rank_rows", "wire_row"]
