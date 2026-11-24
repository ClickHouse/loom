"""Admit refresh tasks and move a schedule cursor in one authority transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from server.core import pg
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.mcp_worker_refresh import RefreshJob, RefreshRejected, RefreshScope
from server.knowledge.domain.mcp_worker_schedule import (
    PeriodicSchedule,
    RefreshDemand,
    ScheduleBinding,
    plan_scheduled_refresh,
)
from server.knowledge.store import pg_worker_refresh

if TYPE_CHECKING:
    from asyncpg import Connection

_KEY = "org = $1 AND namespace = $2 AND source_id = $3"
_CURRENT = f"""
SELECT * FROM loom.mcp_worker_refresh_schedules WHERE {_KEY} FOR UPDATE
"""
_ADVANCE = f"""
UPDATE loom.mcp_worker_refresh_schedules SET next_due_at = $4
WHERE {_KEY} RETURNING 1
"""


@dataclass(frozen=True, repr=False)
class ScheduleAuthority:
    """Exact current source selection, store quota and writer verified under locks."""

    binding: ScheduleBinding
    profile_digest: str
    selected: tuple[str, ...]
    destination_id: str
    owner: str
    actor: str
    agent: str
    session_id: str


def _row_binding(row: dict) -> ScheduleBinding:
    return ScheduleBinding(
        row["org"],
        row["namespace"],
        str(row["source_id"]),
        str(row["connection_id"]),
        str(row["worker_id"]),
        row["connection_generation"],
        row["selection_generation"],
        row["worker_generation"],
    )


def _require_transaction(conn: Connection) -> None:
    if not pg.enabled() or not conn.is_in_transaction():
        raise RefreshRejected("source refresh requires Postgres authority transaction")


async def enqueue_due_on(conn: Connection, authority: ScheduleAuthority) -> int:
    """Use an already authorized and locked source, then lock its schedule and queue rows."""
    _require_transaction(conn)
    binding = authority.binding
    key = (binding.org, binding.namespace, UUID(binding.source_id))
    row = await conn.fetchrow(_CURRENT, *key)
    if row is None or row["user_id"] != authority.owner or row["paused"]:
        return 0
    current = _row_binding(dict(row))
    if current != binding:
        return 0
    schedule = PeriodicSchedule(current, row["interval_seconds"], row["next_due_at"])
    now = await conn.fetchval("SELECT clock_timestamp()")
    plan = plan_scheduled_refresh(schedule, active_binding=binding, now=now)
    if plan is None:
        return 0
    inserted = await enqueue_demand_on(conn, authority, plan.demand)
    if await conn.fetchval(_ADVANCE, *key, plan.next_due_at) != 1:
        raise RefreshRejected("source refresh schedule changed")
    return inserted


async def enqueue_demand_on(
    conn: Connection, authority: ScheduleAuthority, demand: RefreshDemand
) -> int:
    """Enqueue the canonical request under the caller's existing source authority locks."""
    _require_transaction(conn)
    binding = authority.binding
    if binding != demand.binding:
        raise RefreshRejected("source refresh authority changed")
    scope = RefreshScope(
        binding.org,
        binding.namespace,
        authority.actor,
        binding.source_id,
        binding.connection_id,
        authority.destination_id,
        binding.connection_generation,
        binding.selection_generation,
        binding.worker_generation,
        authority.profile_digest,
    )
    job = RefreshJob(demand.job_id, scope, authority.selected)
    inserted = 0
    for task in job.document_tasks():
        entry = pg_worker_refresh.RefreshEntry(
            task,
            UUID(binding.worker_id),
            resource_uri(binding.source_id, task.resource_id),
            authority.owner,
            demand.due_at,
            authority.agent,
            authority.session_id,
        )
        inserted += await pg_worker_refresh.enqueue(conn, entry)
    return inserted
