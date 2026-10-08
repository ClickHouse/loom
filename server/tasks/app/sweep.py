"""The sweeper's transitions (run by ``server.tasks.workers.lease_sweep``)."""

from __future__ import annotations

import json
from datetime import datetime

from server.core.datetimes import utc_now as _now
from server.tasks.app.common import (
    _loads,
    _naive,
    _record,
    _release_lease,
)
from server.tasks.store import repo as repo
from server.tasks.store.rows import InboxRow

SWEEP_ACTOR = "process:task_lease_sweep"


async def sweep_once(*, now: datetime | None = None, limit: int = 200) -> dict[str, int]:
    """Expired leases back to ``todo`` (or ``blocked`` on a human after the
    inbox's ``max_attempts``), deferred tasks past ``resume_at`` back to
    ``todo``, stale triage escalated once. Returns counts per action."""
    at = now or _now()
    naive_now = _naive(at) or at
    found = await repo.sweep_candidates(now=naive_now, limit=limit)
    counts = {"expired": 0, "blocked": 0, "resumed": 0, "escalated": 0}
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
    return counts
