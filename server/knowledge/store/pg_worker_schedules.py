"""Admit refresh tasks and move a schedule cursor in one authority transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from server.core import pg
from server.knowledge.domain.mcp_worker_manual import (
    ManualOffer,
    ManualPosition,
    ManualSelection,
    manual_demand,
    next_offer,
)
from server.knowledge.domain.mcp_worker_refresh import RefreshJob, RefreshRejected, RefreshScope
from server.knowledge.domain.mcp_worker_schedule import (
    PeriodicSchedule,
    RefreshDemand,
    ScheduleBinding,
    ScheduleRejected,
    plan_scheduled_refresh,
)
from server.knowledge.store import pg_worker_refresh, pg_worker_refresh_admission

if TYPE_CHECKING:
    from datetime import datetime

    from asyncpg import Connection, Record

_KEY = "org = $1 AND namespace = $2 AND source_id = $3"
_RECORD_CLOCK = f"""
UPDATE loom.mcp_worker_refresh_admission
SET manual_clock_at = GREATEST(manual_clock_at, $4::timestamptz)
WHERE {_KEY}
"""
_CURRENT = f"""
SELECT * FROM loom.mcp_worker_refresh_schedules WHERE {_KEY} FOR UPDATE
"""
_ADVANCE = f"""
UPDATE loom.mcp_worker_refresh_schedules SET next_due_at = $4
WHERE {_KEY} RETURNING 1
"""


_MANUAL_CLOCK = f"""
UPDATE loom.mcp_worker_refresh_admission
SET manual_clock_at = GREATEST(manual_clock_at, clock_timestamp())
WHERE {_KEY}
RETURNING manual_incarnation, manual_sequence, manual_fingerprint, manual_issued_at,
          manual_expires_at, manual_accepted_items, manual_clock_at
"""
_OFFER = f"""
UPDATE loom.mcp_worker_refresh_admission
SET manual_incarnation = $4, manual_sequence = $5, manual_fingerprint = $6,
    manual_issued_at = $7, manual_expires_at = $8, manual_accepted_items = NULL
WHERE {_KEY} RETURNING 1
"""
_ACCEPT = f"""
UPDATE loom.mcp_worker_refresh_admission
SET manual_accepted_items = $6
WHERE {_KEY} AND manual_incarnation = $4 AND manual_sequence = $5 AND manual_accepted_items IS NULL
  AND manual_expires_at > manual_clock_at
RETURNING 1
"""


@dataclass(frozen=True, repr=False)
class ScheduleAuthority:
    """Exact current source selection and writer verified under the source authority locks."""

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
        row["source_generation"],
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
    await pg_worker_refresh_admission.lock_namespace_on(conn, binding.org, binding.namespace)
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
    inserted = await _enqueue_demand_on(conn, authority, plan.demand)
    if inserted and await conn.fetchval(_ADVANCE, *key, plan.next_due_at) != 1:
        raise RefreshRejected("source refresh schedule changed")
    return inserted


async def _enqueue_demand_on(
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
        binding.source_generation,
        binding.selection_generation,
        binding.worker_generation,
        authority.profile_digest,
    )
    job = RefreshJob(demand.job_id, scope, authority.selected)
    batch = tuple(
        pg_worker_refresh.RefreshEntry(
            task,
            UUID(binding.worker_id),
            authority.owner,
            demand.due_at,
            authority.agent,
            authority.session_id,
        )
        for task in job.document_tasks()
    )
    return await pg_worker_refresh.enqueue_batch_on(conn, batch)


@dataclass(frozen=True, slots=True, repr=False)
class ManualAdmission:
    status: Literal["enqueued", "replayed", "busy", "stale"]
    job_id: str | None = None
    items: int = 0


def _manual_selection(authority: ScheduleAuthority) -> ManualSelection:
    return ManualSelection(
        authority.binding,
        authority.owner,
        authority.actor,
        authority.profile_digest,
        authority.destination_id,
        authority.selected,
    )


async def _manual_slot_on(
    conn: Connection, authority: ScheduleAuthority
) -> tuple[tuple[str, str, UUID], Record]:
    binding = authority.binding
    source_id = UUID(binding.source_id)
    if str(source_id) != binding.source_id or source_id.int == 0:
        raise RefreshRejected("invalid refresh source identity")
    source = pg_worker_refresh_admission.AdmissionSource(
        binding.org,
        binding.namespace,
        source_id,
        authority.agent,
        authority.owner,
        authority.session_id,
        authority.actor,
    )
    key = await pg_worker_refresh_admission.lock_source_on(conn, source)
    row = await conn.fetchrow(_MANUAL_CLOCK, *key)
    if row is None:
        raise RefreshRejected("manual refresh authority changed")
    return key, row


def _offer_from_row(row: Record) -> ManualOffer | None:
    if row["manual_incarnation"] is None:
        return None
    return ManualOffer(
        ManualPosition(row["manual_incarnation"], row["manual_sequence"]),
        row["manual_fingerprint"],
        row["manual_issued_at"],
        row["manual_expires_at"],
        row["manual_accepted_items"],
    )


async def prepare_manual_on(conn: Connection, authority: ScheduleAuthority) -> ManualOffer:
    """Create or return the current server-issued offer without enqueuing work."""
    key, row = await _manual_slot_on(conn, authority)
    previous = _offer_from_row(row)
    position = previous.position if previous else ManualPosition(uuid4(), 0)
    now: datetime = row["manual_clock_at"]
    offer = next_offer(position, previous, _manual_selection(authority), now)
    if offer is previous:
        return offer
    if (
        await conn.fetchval(
            _OFFER,
            *key,
            offer.position.incarnation,
            offer.position.sequence,
            offer.fingerprint,
            offer.issued_at,
            offer.expires_at,
        )
        != 1
    ):
        raise RefreshRejected("manual refresh authority changed")
    return offer


class _ExpiredOffer(Exception):
    def __init__(self, observed_at: datetime) -> None:
        self.observed_at = observed_at


async def _stage_manual_on(
    conn: Connection,
    authority: ScheduleAuthority,
    key: tuple[str, str, UUID],
    requested: ManualPosition,
    offer: ManualOffer,
    demand: RefreshDemand,
) -> ManualAdmission:
    async with conn.transaction():
        inserted = await _enqueue_demand_on(conn, authority, demand)
        observed = await conn.fetchrow(_MANUAL_CLOCK, *key)
        if observed is None:
            raise RefreshRejected("manual refresh authority changed")
        if not offer.current(observed["manual_clock_at"]):
            raise _ExpiredOffer(observed["manual_clock_at"])
        if not inserted:
            return ManualAdmission("busy")
        if (
            await conn.fetchval(_ACCEPT, *key, requested.incarnation, requested.sequence, inserted)
            != 1
        ):
            raise RefreshRejected("manual refresh authority changed")
        return ManualAdmission("enqueued", demand.job_id, inserted)


async def submit_manual_on(
    conn: Connection, authority: ScheduleAuthority, requested: ManualPosition
) -> ManualAdmission:
    """Admit once by a persisted offer; submit never creates or rotates an offer."""
    _require_transaction(conn)
    key, row = await _manual_slot_on(conn, authority)
    offer = _offer_from_row(row)
    if offer is None:
        return ManualAdmission("stale")
    try:
        demand = manual_demand(
            offer, requested, _manual_selection(authority), now=row["manual_clock_at"]
        )
    except ScheduleRejected:
        return ManualAdmission("stale")
    if offer.accepted_items is not None:
        return ManualAdmission("replayed", demand.job_id, offer.accepted_items)
    try:
        return await _stage_manual_on(conn, authority, key, requested, offer, demand)
    except _ExpiredOffer as expired:
        # Keep the observation even though the tentative batch was rolled back.
        await conn.execute(_RECORD_CLOCK, *key, expired.observed_at)
        return ManualAdmission("stale")
