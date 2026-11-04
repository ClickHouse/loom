"""The ingestion queue on a real ClickHouse: one namespace's backlog takes turns
with another's (each offers one due job, the namespace served longest ago, or
never, first), one source's jobs run one at a time in the order asked, and each
namespace offers only the head of its sources."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.asyncio
async def test_a_backlog_takes_turns_with_another_namespaces_job(ch_client: Any) -> None:
    from server.core.ch import get_client
    from server.knowledge.store import ingestions

    busy, quiet = unique_org("ingest-busy"), unique_org("ingest-quiet")
    older = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10)

    def job(org: str, state: str, at: datetime) -> dict[str, Any]:
        return ingestions.ingestion_row(
            org=org,
            namespace="docs",
            ingestion_id=str(uuid.uuid4()),
            source_uri="repo://a",
            upload_id=str(uuid.uuid4()),
            sha256="a" * 64,
            state=state,
            created_at=at,
            next_attempt_at=at,
        )

    backlog = [job(busy, "pending", older + timedelta(seconds=i)) for i in range(3)]
    rows = [
        *backlog,
        job(busy, "ready", older),
        job(quiet, "pending", older + timedelta(minutes=5)),
    ]
    ch = get_client()
    await ch.connect()
    try:
        for row in rows:
            await ingestions.insert_ingestion(row)
        due = [
            d for d in await ingestions.due(1000, stale_after_s=120) if d["org"] in (busy, quiet)
        ]
    finally:
        await ch.disconnect()
    # The busy namespace just ran a job, so the quiet one's later job goes first,
    # and the backlog offers only its oldest.
    assert [(d["org"], d["ingestion_id"]) for d in due] == [
        (quiet, rows[-1]["ingestion_id"]),
        (busy, backlog[0]["ingestion_id"]),
    ]


@pytest.mark.asyncio
async def test_one_sources_jobs_run_one_at_a_time_in_order(ch_client: Any) -> None:
    """Only the oldest open job of a source is offered: a later ingest of the
    source waits while the earlier one runs, and a stale earlier one (its
    worker gone) runs again before it."""
    from server.core.ch import get_client
    from server.knowledge.store import ingestions

    org = unique_org("ingest-order")
    now = datetime.now(UTC).replace(tzinfo=None)

    def job(state: str, created: datetime, updated: datetime) -> dict[str, Any]:
        row = ingestions.ingestion_row(
            org=org,
            namespace="docs",
            ingestion_id=str(uuid.uuid4()),
            source_uri="repo://same",
            upload_id=str(uuid.uuid4()),
            sha256="a" * 64,
            state=state,
            created_at=created,
            next_attempt_at=created,
        )
        return {**row, "updated_at": updated}

    earlier = job("processing", now - timedelta(minutes=10), now)  # a live worker runs it
    later = job("pending", now - timedelta(minutes=5), now - timedelta(minutes=5))
    ch = get_client()
    await ch.connect()
    try:
        for row in (earlier, later):
            await ingestions.insert_ingestion(row)

        async def offered() -> list[str]:
            due = await ingestions.due(1000, stale_after_s=120)
            return [d["ingestion_id"] for d in due if d["org"] == org]

        assert await offered() == [], "the later job waits while the earlier one runs"
        stale = {
            **earlier,
            "updated_at": now - timedelta(minutes=9),
            "version": earlier["version"] + 1,
        }
        await ingestions.insert_ingestion(stale)
        assert await offered() == [earlier["ingestion_id"]]
    finally:
        await ch.disconnect()


@pytest.mark.asyncio
async def test_each_namespace_offers_one_due_job_at_the_head_of_its_source(ch_client: Any) -> None:
    """One candidate per namespace, the job due longest of its sources' first open
    jobs; a source's later job waits behind the earlier one even while that one is
    not due (a retry's delay). The rows are ended after, so none stays on the queue."""
    from server.core.ch import get_client
    from server.knowledge.store import ingestions

    org = unique_org("ingest-heads")
    now = datetime.now(UTC).replace(tzinfo=None)

    def job(ns: str, uri: str, created: datetime, due_at: datetime | None = None) -> dict[str, Any]:
        return ingestions.ingestion_row(
            org=org,
            namespace=ns,
            ingestion_id=str(uuid.uuid4()),
            source_uri=uri,
            upload_id=str(uuid.uuid4()),
            sha256="a" * 64,
            state="pending",
            created_at=created,
            next_attempt_at=due_at or created,
        )

    rows = [
        # `one`'s first job waits out a retry for an hour; its later job stays behind it.
        job("a", "repo://one", now - timedelta(minutes=10), now + timedelta(hours=1)),
        job("a", "repo://one", now - timedelta(minutes=9)),
        job("a", "repo://two", now - timedelta(minutes=8)),
        job("a", "repo://three", now - timedelta(minutes=7)),
        job("b", "repo://one", now - timedelta(minutes=6)),
    ]
    ch = get_client()
    await ch.connect()
    try:
        for row in rows:
            await ingestions.insert_ingestion(row)
        due = await ingestions.due(100_000, stale_after_s=120)
        for row in rows:
            await ingestions.insert_ingestion(ingestions.ingestion_row(row, state="cancelled"))
    finally:
        await ch.disconnect()
    offered = sorted((d["namespace"], d["ingestion_id"]) for d in due if d["org"] == org)
    # Without the per-namespace rule `a` would offer `three` too; without the per-source
    # one, the waiting source's later job (due before `two`) would be offered.
    assert offered == [("a", rows[2]["ingestion_id"]), ("b", rows[4]["ingestion_id"])]
