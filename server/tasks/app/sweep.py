"""The sweeper's transitions (run by ``server.tasks.workers.lease_sweep``)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from server.core.datetimes import utc_now as _now
from server.tasks.app.common import (
    TERMINAL,
    _loads,
    _naive,
    _record,
    _release_lease,
)
from server.tasks.store import repo as repo

log = logging.getLogger(__name__)
from server.tasks.store.rows import InboxRow

SWEEP_ACTOR = "process:task_lease_sweep"
# A lease row or task written this recently may belong to a transition still in flight
# (a create between its task row and its lease row, a reopen between its two writes).
LEASE_RECONCILE_GRACE = timedelta(seconds=60)


async def sweep_once(*, now: datetime | None = None, limit: int = 200) -> dict[str, int]:
    """Expired leases back to ``todo`` (or ``blocked`` on a human after the
    inbox's ``max_attempts``), deferred tasks past ``resume_at`` back to
    ``todo``, stale triage escalated once, and lease rows that disagree with
    their task put right (`reconcile_leases`). Returns counts per action."""
    at = now or _now()
    naive_now = _naive(at) or at
    found = await repo.sweep_candidates(now=naive_now, limit=limit)
    counts = {"expired": 0, "blocked": 0, "resumed": 0, "escalated": 0, "leases": 0}
    inboxes: dict[tuple[str, str], InboxRow | None] = {}

    async def _inbox(org: str, name: str) -> InboxRow | None:
        k = (org, name)
        if k not in inboxes:
            inboxes[k] = await repo.fetch_inbox_row(org, name)
        return inboxes[k]

    for task in found["expired"]:
        inbox = await _inbox(task["org"], task["inbox"])
        max_attempts = int((inbox["max_attempts"] if inbox else 0) or 3)
        await _release_lease(task, status="todo")
        if int(task.get("attempt") or 0) >= max_attempts:
            await _record(
                task,
                event="lease_expired",
                actor=SWEEP_ACTOR,
                agent="task_lease_sweep",
                reason=f"lease expired {task.get('attempt')} times; owner {task.get('owner')!r}",
                status="blocked",
                blocked_on="human",
                owner="",
                lease_until=None,
            )
            counts["blocked"] += 1
        else:
            await _record(
                task,
                event="lease_expired",
                actor=SWEEP_ACTOR,
                agent="task_lease_sweep",
                reason=f"lease of {task.get('owner')!r} expired",
                status="todo",
                owner="",
                lease_until=None,
            )
            counts["expired"] += 1
    for task in found["resume"]:
        await _record(
            task,
            event="resumed",
            actor=SWEEP_ACTOR,
            agent="task_lease_sweep",
            status="todo",
            resume_at=None,
            reason="",
        )
        counts["resumed"] += 1
    for stale in found["stale_triage"]:
        stale_task = await repo.fetch_task(stale["org"], str(stale["task_id"]))
        if stale_task is None or stale_task["status"] != "triage":
            continue
        outcome = _loads(stale_task.get("outcome"), {})
        if outcome.get("escalated_at"):
            continue
        outcome["escalated_at"] = naive_now.isoformat()
        await _record(
            stale_task,
            event="triage_escalated",
            actor=SWEEP_ACTOR,
            agent="task_lease_sweep",
            reason=f"unreviewed for more than {stale['after_s']} s",
            outcome=json.dumps(outcome, sort_keys=True, default=str),
        )
        counts["escalated"] += 1
    counts["leases"] = await reconcile_leases(naive_now, limit=limit)
    return counts


async def reconcile_leases(now: datetime, *, limit: int) -> int:
    """The lease is a claim's only arbiter, and its rows are written from status reads
    nobody holds, so a race can leave one that disagrees with its task. Each pass makes
    three repairs, past the grace:

    - a todo or triage task with no lease row gets one at its fence;
    - an idle or expired row of a task that is gone or ended is deleted;
    - an idle or expired row at another fence than its task's (a claim cut off after
      winning holds one a fence ahead) is given back idle at the task's fence.

    Every write is guarded (insert if absent; update or delete only while the row is
    still held as read), so a pass racing a claim or a second copy of itself changes
    nothing it should not."""
    backend = await repo.lease_backend()
    missing, stray = await repo.lease_strays(
        before=now - LEASE_RECONCILE_GRACE, terminal=sorted(TERMINAL), limit=limit
    )
    repaired = 0
    for task in missing:
        try:
            await backend.ensure_row(
                task["task_id"], task["org"], task["inbox"], int(task["fence"])
            )
            repaired += 1
        except Exception as e:  # noqa: BLE001 — one stray row never stops the reconcile
            log.warning("task_lease_sweep: lease row of %s not written: %s", task["task_id"], e)
    for row in stray:
        held = int(row["lease_fence"])
        try:
            if not row["found"] or row["status"] in TERMINAL:
                await backend.delete_held(row["task_id"], row["owner"], fence=held)
            else:
                await backend.give_back(
                    row["task_id"], row["owner"], held_fence=held, fence=int(row["fence"])
                )
            repaired += 1
        except Exception as e:  # noqa: BLE001 — one stray row never stops the reconcile
            log.warning("task_lease_sweep: lease row of %s not repaired: %s", row["task_id"], e)
    return repaired
