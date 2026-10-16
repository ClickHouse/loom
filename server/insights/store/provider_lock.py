"""Cross-process serialization of short operator-configuration writes."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from server.core import leases
from server.core.ch import sequential_reads
from server.core.provider_process import process_identity
from server.core.repo_base import _default_client
from server.insights.store.provider_participants import (
    ONE_SQL,
    participant_sync,
    running_queries,
    running_queries_sync,
)

LOCK_NAME = "provider_configuration"
LOCK_TTL_S = 60
LOCK_CLEAR = (
    "ALTER TABLE loom._migration_lock DELETE WHERE name = {name:String} "
    "AND holder = {old:String} SETTINGS keeper_map_strict_mode = 1"
)


async def holder_stopped(holder: str) -> bool:
    """The holder's participant retired, or its heartbeat expired, and no query
    tagged with its id still runs. A holder with no participant row (or one from
    a build without heartbeats) never counts as stopped: `loom provider-lock clear`.
    An expired lease alone is no proof: a paused holder may resume and write."""
    participant = holder.split("/", 1)[0]
    with sequential_reads():
        row = await _default_client().query_one(ONE_SQL, {"id": participant})
    if row is None or not (row["state"] == "retired" or row["expired"]):
        return False
    return await running_queries(participant) == 0


def clear_holder(ch: Any, holder: str) -> str:
    """`cleared`, `absent` (the lock is free or held by someone else), `live` (it
    renewed within its TTL) or `running` (a query tagged with its id still runs)."""
    rows = ch.query(leases.LOCK_HOLDER, parameters={"name": LOCK_NAME}).result_rows
    if not rows or str(rows[0][0]) != holder:
        return "absent"
    if not bool(rows[0][3]):
        return "live"
    participant = holder.split("/", 1)[0]
    row = participant_sync(ch, participant)
    if row is not None and row["stale_after_s"] > 0 and not row["expired"]:
        return "live"
    if running_queries_sync(ch, participant) != 0:
        return "running"
    ch.command(LOCK_CLEAR, parameters={"name": LOCK_NAME, "old": holder})
    return "cleared"


class ConfigurationLockUnavailable(RuntimeError):
    """The shared configuration lock could not be acquired."""


@asynccontextmanager
async def configuration_lock(*, wait_s: float = 5) -> AsyncIterator[leases.Lease]:
    """Hold the provider configuration's lease for the block; once it is lost the
    block is cancelled (`leases.LeaseLost`).

    Never a process-local lock: two containers could then overwrite one another's
    keys after both passed an expected-version check. An expired key is taken only
    from a holder whose process stopped. Validation is performed before entering
    this short critical section.
    """
    try:
        lease = await leases.take(
            LOCK_NAME,
            ttl_s=LOCK_TTL_S,
            wait_s=wait_s,
            poll_s=0.5,
            poll_max_s=5.0,
            holder=f"{process_identity()}/{uuid4()}",
            may_take_over=holder_stopped,
        )
    except Exception as e:
        raise ConfigurationLockUnavailable("provider_configuration_lock_unavailable") from e
    if lease is None:
        raise ConfigurationLockUnavailable("provider_configuration_lock_unavailable")
    async with lease.renewed():
        yield lease
