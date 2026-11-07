"""Point-in-time rollback of a namespace's memory heads.

Pure planning over two views of the same keys:

* the head each key has NOW (loom.memory FINAL), and
* the highest version each key had AT OR BEFORE the target instant
  (loom.memory_versions, `version <= version_at`).

`version` on loom.memory is a wall-clock epoch-ns stamp taken at write
time (`memory_repo.next_version`), so "the head as of T" is exactly "the
largest version ≤ T_ns". That is head-state rollback: each key returns to
the row it had at T. It is not exact-state rollback — derived aggregates
and embeddings are regenerated from the reinstated heads by
the existing workers, not restored byte-for-byte. The planner never sees
ClickHouse; the handler feeds it join rows and applies the plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def version_at(as_of: datetime) -> int:
    """Epoch-ns version threshold for `as_of` (naive values are UTC)."""
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    return int(as_of.timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class RollbackAction:
    """One key's move. `action` is `reinstate` (re-insert the version the
    key had at T as the new head) or `remove` (the key did not exist at T
    and is live now: tombstone it)."""

    agent: str
    user_id: str
    session_id: str
    memory_key: str
    action: str
    version_from: int
    version_to: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_key": self.memory_key,
            "agent": self.agent,
            "action": self.action,
            "version_from": self.version_from,
            "version_to": self.version_to,
        }


@dataclass
class RollbackPlan:
    reinstate: list[RollbackAction] = field(default_factory=list)
    remove: list[RollbackAction] = field(default_factory=list)
    # Keys whose versions exist at T but that have no head row now: the row
    # was hard-deleted (erasure) — a rollback must not resurrect it.
    skipped_no_head: int = 0
    # Keys created after T that are already tombstoned: nothing to do.
    skipped_already_gone: int = 0

    @property
    def changes(self) -> int:
        return len(self.reinstate) + len(self.remove)


def plan_rollback(rows: list[dict[str, Any]]) -> RollbackPlan:
    """Turn the FULL OUTER JOIN of heads-now and heads-at-T into a plan.

    Each row carries the identity tuple + memory_key and:
      version_now      head version now, 0 when the key has no head row
      tombstoned_now   1 when that head is a tombstone
      version_at       highest version ≤ T, 0 when the key did not exist at T
    Rows where the two versions agree are already filtered by the query but
    are tolerated here.
    """
    plan = RollbackPlan()
    for r in rows:
        v_now = int(r.get("version_now") or 0)
        v_at = int(r.get("version_at") or 0)
        tomb_now = bool(int(r.get("tombstoned_now") or 0))
        ident = (
            str(r.get("agent") or ""),
            str(r.get("user_id") or "-"),
            str(r.get("session_id") or "-"),
            str(r.get("memory_key") or ""),
        )
        if v_at == v_now:
            continue
        if v_at == 0:
            # Did not exist at T.
            if v_now == 0 or tomb_now:
                plan.skipped_already_gone += 1
                continue
            plan.remove.append(RollbackAction(*ident, "remove", v_now, 0))
            continue
        if v_now == 0:
            plan.skipped_no_head += 1
            continue
        plan.reinstate.append(RollbackAction(*ident, "reinstate", v_now, v_at))
    return plan
