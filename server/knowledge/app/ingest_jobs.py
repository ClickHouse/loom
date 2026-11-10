"""Where a knowledge ingestion job is kept: loom.knowledge_jobs under the Postgres
authority (`pg_jobs`), loom.knowledge_ingestions otherwise.

Under Postgres, a build that predates loom.knowledge_jobs may still run beside this one
and keeps its jobs in ClickHouse only: a job it queued, ran or cancelled. So a read takes
the newer version of the two stores, the queue offers ClickHouse's due jobs too, and a
cancel is written to both and read from either. A run of a job Postgres holds first
takes ClickHouse's version when it is the newer; a job only ClickHouse holds runs there,
as under the ClickHouse authority (`ingest_run._take`; rolling upgrades, rule 40).
"""

from __future__ import annotations

import itertools
from typing import Any

from server.core import pg
from server.core.ch import sequential_reads
from server.knowledge.store import ingestions, pg_jobs

# ClickHouse's earlier open jobs of a source `earlier_open` reads at most.
_EARLIER_PAGE = 100  # not a setting: a source with more waits until they are repaired or run
# The open jobs, and the newest-saved non-pending ones, `unprojected` compares at most, each.
UNPROJECTED_PAGE = 1_000  # not a setting: a report's bound, said when reached
# Which store's due jobs lead this process's next pass (`due`).
_passes = itertools.count()  # not a setting: only the order of the two lists


def _newer(held: dict[str, Any] | None, projected: dict[str, Any] | None) -> dict[str, Any] | None:
    if held is None or projected is None:
        return held or projected
    return projected if int(projected["version"]) > int(held["version"]) else held


async def both(
    org: str, namespace: str, ingestion_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Under Postgres, the job as each store holds it: (Postgres's, ClickHouse's)."""
    held = await pg_jobs.get(org, namespace, ingestion_id)
    with sequential_reads():  # a version the older build wrote on another replica
        projected = await ingestions.get_ingestion(org, namespace, ingestion_id)
    return held, projected


async def get(org: str, namespace: str, ingestion_id: str) -> dict[str, Any] | None:
    """The job as it is now; None when neither store holds it."""
    if not pg.enabled():
        return await ingestions.get_ingestion(org, namespace, ingestion_id)
    return _newer(*await both(org, namespace, ingestion_id))


async def earlier_open(job: dict[str, Any]) -> bool:
    """Under Postgres, whether an earlier open job of `job`'s source runs first: one
    Postgres holds, or one only ClickHouse holds (an older build queued it). Postgres
    decides for every job it holds, so a projection that did not land changes nothing.
    ClickHouse's oldest `_EARLIER_PAGE` are read, and a full page is taken as one to wait
    for (usually projections behind ended jobs, each repaired when offered)."""
    if await pg_jobs.earlier_open(job):
        return True
    with sequential_reads():
        earlier = await ingestions.older_open(job, limit=_EARLIER_PAGE)
    if len(earlier) == _EARLIER_PAGE:
        return True
    return bool(set(earlier) - await pg_jobs.held_ids(job["org"], job["namespace"], earlier))


async def unprojected(org: str, namespace: str) -> tuple[int, bool]:
    """Under Postgres, of the namespace's open jobs and most recently saved non-pending
    ones (at most `UNPROJECTED_PAGE` each), how many ClickHouse holds at an older
    version or not at all, and whether the namespace holds more than were compared. A
    version whose projection did not land stays behind until the job's next save; once
    the job ended, until a run of the copy ClickHouse holds open projects it again
    (`ingest_run._take`), or, with no copy there, until the job expires."""
    held, more = await pg_jobs.versions(org, namespace, UNPROJECTED_PAGE)
    # Default consistency, as the job sections read: on one replica a lag overcounts only.
    projected = await ingestions.versions(org, namespace, list(held))
    behind = sum(1 for job, version in held.items() if projected.get(job, -1) < version)
    return behind, more


async def submit(row: dict[str, Any]) -> None:
    if pg.enabled():
        await pg_jobs.insert(row)
    else:
        await ingestions.insert_ingestion(row)


async def request_cancel(
    org: str, namespace: str, ingestion_id: str, author: dict[str, str]
) -> None:
    """Record a cancel of the job. ClickHouse's row is the older build's and names who
    asked; Postgres marks the job itself."""
    await ingestions.insert_cancel(org, namespace, ingestion_id, author)
    if pg.enabled():
        await pg_jobs.request_cancel(org, namespace, ingestion_id)


async def cancel_requested(org: str, namespace: str, ingestion_id: str) -> bool:
    if pg.enabled() and await pg_jobs.cancel_requested(org, namespace, ingestion_id):
        return True
    return await ingestions.cancel_requested(org, namespace, ingestion_id)


async def due(limit: int, *, stale_after_s: float) -> list[dict[str, Any]]:
    """The worker's queue (`ingestions.due`). Under Postgres its own queue and
    ClickHouse's due jobs it does not offer (an older build's, or a projection behind
    its job), up to `limit` from each and each one per namespace, interleaved; the two
    lead in turn from one pass to the next, so neither waits out the other's backlog. A
    ClickHouse job whose source holds a later job Postgres offers is offered too, and
    that later job waits for it (`ingest_run._take`)."""
    projected = await ingestions.due(limit, stale_after_s=stale_after_s)
    if not pg.enabled():
        return projected
    held = await pg_jobs.due(limit, stale_after_s=stale_after_s)
    offered = {(j["org"], j["namespace"], j["ingestion_id"]) for j in held}
    rest = [j for j in projected if (j["org"], j["namespace"], j["ingestion_id"]) not in offered]
    first, second = (held, rest) if next(_passes) % 2 == 0 else (rest, held)
    return [j for pair in itertools.zip_longest(first, second) for j in pair if j is not None]
