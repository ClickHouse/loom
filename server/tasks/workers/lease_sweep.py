"""Task inbox sweeper.

One pass: leases past ``lease_until`` go back to ``todo`` (``attempt + 1``),
or to ``blocked`` on a human after the inbox's ``max_attempts``; deferred
tasks past ``resume_at`` go back to ``todo``; a triage row older than its
inbox's escalation threshold is escalated once. Every action is a
``task_events`` row under the actor ``process:task_lease_sweep``. Last, lease
rows that disagree with their task are put right (no event: the task does not
change). The supervisor runs the pass in the lease-holding process only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SweepResult:
    expired: int
    blocked: int
    resumed: int
    escalated: int
    leases: int

    @property
    def total(self) -> int:
        return self.expired + self.blocked + self.resumed + self.escalated + self.leases


async def run_pass() -> SweepResult:
    from server.tasks.app.sweep import sweep_once

    with record_pass("task_lease_sweep") as rec:
        counts = await sweep_once()
        rec.outcome = dict(counts)
    result = SweepResult(**counts)
    if result.total == 0:
        pass_context.mark_idle()
    else:
        log.info(
            "task_lease_sweep: expired=%d blocked=%d resumed=%d escalated=%d leases=%d",
            result.expired,
            result.blocked,
            result.resumed,
            result.escalated,
            result.leases,
        )
    return result
