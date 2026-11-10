"""Knowledge ingestion jobs under the Postgres authority (`loom.knowledge_jobs`), on
the stores a server run with LOOM_STORAGE_AUTHORITY=postgres uses: each save fenced by
its run's token and projected into ClickHouse's loom.knowledge_ingestions, the queue,
a cancel, the adoption of an older build's version, the erasure step and the expiry.
Skipped against a server of the ClickHouse authority.

The server under test may run the ingest worker on the same stores: a job a test does
not mean to run is due hours ahead, and one it makes due is held under its job lease.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def stores(control_plane_url: str) -> AsyncIterator[None]:
    """The Postgres authority and ClickHouse, for the job store's code in this process;
    skips against a server of the ClickHouse authority."""
    from server.core import pg
    from server.core.ch import get_client

    os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10) as c:
        health = (await c.get("/v1/health")).json()
    if health.get("storage_authority") != "postgres":
        pytest.skip("the server under test is not of the Postgres authority")
    assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"
    ch = get_client()
    await ch.connect()
    try:
        yield
    finally:
        await ch.disconnect()
        await pg.close_pool()


def _job(org: str, **fields: Any) -> dict[str, Any]:
    """A pending job no worker takes: its next attempt is hours ahead."""
    from server.knowledge.store import ingestions

    later = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=6)
    return ingestions.ingestion_row(
        **{
            "org": org,
            "namespace": "docs",
            "ingestion_id": str(uuid.uuid4()),
            "source_uri": "repo://a.sql",
            "upload_id": str(uuid.uuid4()),
            "sha256": "a" * 64,
            "state": "pending",
            "next_attempt_at": later,
            **fields,
        }
    )


def _key(row: dict[str, Any]) -> tuple[str, str, str]:
    return row["org"], row["namespace"], row["ingestion_id"]


async def _projected(row: dict[str, Any]) -> dict[str, Any] | None:
    from server.core.ch import sequential_reads
    from server.knowledge.store import ingestions

    with sequential_reads():
        return await ingestions.get_ingestion(*_key(row))


async def _end(*rows: dict[str, Any]) -> None:
    """End jobs a test leaves open, in both stores, so no worker runs them later."""
    from server.knowledge.store import ingestions, pg_jobs

    for row in rows:
        ended = ingestions.ingestion_row(row, state="cancelled")
        taken = await pg_jobs.take(*_key(row)) if await pg_jobs.get(*_key(row)) else None
        if taken is not None:
            await pg_jobs.save(ended, taken[0])  # projected with it
        else:
            await ingestions.insert_ingestion(ended)


def _same(read: dict[str, Any] | None, row: dict[str, Any]) -> None:
    """Every column as written; ClickHouse keeps datetimes to the millisecond."""
    assert read is not None
    for column, value in row.items():
        if isinstance(value, datetime):
            assert abs(read[column] - value) < timedelta(milliseconds=1), column
        else:
            assert read[column] == value, column


async def test_a_job_is_saved_under_its_run_s_token_and_projected(stores: None) -> None:
    from server.knowledge.store import ingestions, pg_jobs

    row = _job(unique_org("pgjobs"), title="a\x00title", tags=["t"])
    await pg_jobs.insert(row)
    assert await pg_jobs.get(*_key(row)) == row
    _same(await _projected(row), row)

    taken = await pg_jobs.take(*_key(row))
    assert taken is not None and taken[1] == row
    token = taken[0]
    again = await pg_jobs.take(*_key(row))  # another run took it since
    assert again is not None and again[0] == token + 1

    stale = ingestions.ingestion_row(row, state="processing", stage="parse")
    assert await pg_jobs.save(stale, token) == "taken"
    assert await pg_jobs.get(*_key(row)) == row
    saved = ingestions.ingestion_row(row, state="processing", stage="parse")
    assert await pg_jobs.save(saved, again[0]) == "saved"
    assert await pg_jobs.get(*_key(row)) == saved
    _same(await _projected(row), saved)
    await _end(row)


async def test_a_cancel_is_read_back(stores: None) -> None:
    from server.knowledge.store import pg_jobs

    row = _job(unique_org("pgjobs-cancel"))
    await pg_jobs.insert(row)
    assert not await pg_jobs.cancel_requested(*_key(row))
    await pg_jobs.request_cancel(*_key(row))
    assert await pg_jobs.cancel_requested(*_key(row))
    await _end(row)


async def test_an_older_build_s_version_is_adopted_only_when_newer(stores: None) -> None:
    from server.knowledge.store import ingestions, pg_jobs

    row = _job(unique_org("pgjobs-adopt"))
    await pg_jobs.adopt(row)  # a job Postgres does not hold is never inserted
    assert await pg_jobs.get(*_key(row)) is None
    await pg_jobs.insert(row)
    newer = ingestions.ingestion_row(row, state="failed", error_code="no_text")
    await pg_jobs.adopt(newer)
    assert await pg_jobs.get(*_key(row)) == newer
    await pg_jobs.adopt(row)  # older: kept as it is
    assert await pg_jobs.get(*_key(row)) == newer
    await _end(row)  # ClickHouse still holds the pending version


async def test_the_queue_offers_a_due_job_and_a_stale_run(stores: None) -> None:
    from server.core import leases
    from server.knowledge.store import pg_jobs

    org = unique_org("pgjobs-due")
    now = datetime.now(UTC).replace(tzinfo=None)
    running = {
        **_job(org, state="processing", created_at=now - timedelta(minutes=10)),
        "updated_at": now,
    }
    # Due, but behind the source's earlier job.
    later = _job(org, created_at=now - timedelta(minutes=5), next_attempt_at=now)
    other = _job(org, source_uri="repo://b.sql")
    for row in (running, later, other):
        await pg_jobs.insert(row)

    async def offered() -> list[str]:
        due = await pg_jobs.due(1000, stale_after_s=120)
        return [d["ingestion_id"] for d in due if d["org"] == org]

    lease = await leases.take(f"knowledge-ingest:{running['ingestion_id']}", ttl_s=60)
    assert lease is not None  # no worker runs the job once it reads as stale
    try:
        # The source's earlier job runs, so its later one waits; the other source's is
        # not due yet.
        assert await offered() == []
        stale = {**running, "updated_at": now - timedelta(minutes=9)}
        taken = await pg_jobs.take(*_key(running))
        assert taken is not None
        assert await pg_jobs.save(stale, taken[0]) == "saved"
        assert await offered() == [running["ingestion_id"]]
    finally:
        # `later` first: once `running` ends, `later` would be its source's due head.
        await _end(later, running, other)
        await lease.release()  # once the job is ended


async def test_an_erasure_deletes_the_jobs_and_a_run_then_saves_nothing(stores: None) -> None:
    from server.erasure.store import steps_pg
    from server.knowledge.store import ingestions, pg_jobs

    org = unique_org("pgjobs-erase")
    row = _job(org)
    await pg_jobs.insert(row)
    taken = await pg_jobs.take(*_key(row))
    assert taken is not None
    assert await steps_pg.pg_holds_namespace(org, "docs")
    assert await steps_pg.delete_pg_knowledge_by_namespace(org, "docs") == 1
    assert await pg_jobs.get(*_key(row)) is None
    later = ingestions.ingestion_row(row, state="processing")
    assert await pg_jobs.save(later, taken[0]) == "gone"
    residue = await steps_pg.pg_residue(org, "namespace", "docs")
    assert residue["pg:loom.knowledge_jobs"] == 0
    await _end(row)  # the projection, which documents.tombstone would delete


async def test_an_erased_job_is_never_held_again(stores: None) -> None:
    """Between the Postgres step and `documents.tombstone`, ClickHouse still holds the
    erased job: a run reads it there and Postgres holds nothing of it again."""
    from server.erasure.store import steps_pg
    from server.knowledge.app import ingest_run
    from server.knowledge.store import pg_jobs

    org = unique_org("pgjobs-erased")
    row = _job(org)
    await pg_jobs.insert(row)
    await steps_pg.delete_pg_knowledge_by_namespace(org, "docs")
    current = await ingest_run._take(row)
    assert current is not None and current["ingestion_id"] == row["ingestion_id"]
    assert ingest_run._RUN_TOKEN.get() is None  # its saves go to ClickHouse alone
    assert await pg_jobs.get(*_key(row)) is None
    residue = await steps_pg.pg_residue(org, "namespace", "docs")
    assert residue["pg:loom.knowledge_jobs"] == 0
    await _end(row)


async def test_an_expired_job_is_neither_offered_nor_waited_on(stores: None) -> None:
    """Before the expiry pass deletes it, a job no run has saved for 30 days is not its
    source's head, and a later job of the source does not wait for it."""
    from server.core import leases
    from server.core.ch import sequential_reads
    from server.knowledge.store import ingestions, pg_jobs

    org = unique_org("pgjobs-stale")
    now = datetime.now(UTC).replace(tzinfo=None)
    expired = {
        **_job(org, created_at=now - timedelta(days=40)),
        "updated_at": now - timedelta(days=31),
    }
    later = _job(org, created_at=now - timedelta(hours=1), next_attempt_at=now)
    lease = await leases.take(f"knowledge-ingest:{later['ingestion_id']}", ttl_s=60)
    assert lease is not None  # no worker runs the due job meanwhile
    try:
        for row in (expired, later):
            await pg_jobs.insert(row)
        due = [d["ingestion_id"] for d in await pg_jobs.due(1000, stale_after_s=120)]
        assert later["ingestion_id"] in due and expired["ingestion_id"] not in due
        assert not await pg_jobs.earlier_open(later)
        # ClickHouse's copy too, which its TTL drops only at a later merge.
        with sequential_reads():
            offered = [d["ingestion_id"] for d in await ingestions.due(1000, stale_after_s=120)]
            assert expired["ingestion_id"] not in offered
            assert await ingestions.older_open(later, limit=100) == []
    finally:
        await _end(later, expired)
        await lease.release()


async def test_the_expiry_deletes_a_job_no_run_saved_for_30_days(stores: None) -> None:
    from server.knowledge.store import pg_jobs

    org = unique_org("pgjobs-expire")
    now = datetime.now(UTC).replace(tzinfo=None)
    old = {**_job(org), "updated_at": now - timedelta(days=31)}
    recent = {**_job(org), "updated_at": now - timedelta(days=29)}
    for row in (old, recent):
        await pg_jobs.insert(row)
    await pg_jobs.expire(1000)  # the server's own pass may have expired it already
    assert await pg_jobs.get(*_key(old)) is None
    assert await pg_jobs.get(*_key(recent)) == recent
    await _end(old, recent)


async def test_a_job_behind_an_earlier_open_one_of_its_source_is_told(stores: None) -> None:
    """`ingestions.older_open`, which holds a job Postgres holds behind an earlier one
    only ClickHouse holds."""
    from server.core.ch import sequential_reads
    from server.knowledge.store import ingestions

    org = unique_org("pgjobs-older")
    now = datetime.now(UTC).replace(tzinfo=None)
    earlier = _job(org, created_at=now - timedelta(minutes=2))
    later = _job(org, created_at=now - timedelta(minutes=1))
    elsewhere = _job(org, source_uri="repo://b.sql", created_at=now - timedelta(minutes=3))
    for row in (earlier, later, elsewhere):
        await ingestions.insert_ingestion(row)
    with sequential_reads():
        assert await ingestions.older_open(later, limit=100)
        assert not await ingestions.older_open(earlier, limit=100)
        await ingestions.insert_ingestion(ingestions.ingestion_row(earlier, state="failed"))
        assert not await ingestions.older_open(later, limit=100)
    await ingestions.insert_ingestion(ingestions.ingestion_row(later, state="cancelled"))
    await ingestions.insert_ingestion(ingestions.ingestion_row(elsewhere, state="cancelled"))


async def test_an_open_job_keeps_its_original_from_the_sweep(stores: None) -> None:
    """The sweep reads the open jobs' originals from Postgres, whether or not their
    projection landed; an ended job keeps none."""
    from server.knowledge.store import ingestions, pg_documents, pg_jobs

    org = unique_org("pgjobs-owed")
    row = _job(org, sha256="b" * 64)
    await pg_jobs.insert(row)
    assert "b" * 64 in await pg_documents.owed_originals(org, "docs")
    taken = await pg_jobs.take(*_key(row))
    assert taken is not None
    await pg_jobs.save(ingestions.ingestion_row(row, state="failed"), taken[0])
    assert "b" * 64 not in await pg_documents.owed_originals(org, "docs")


async def test_the_queue_serves_the_namespace_served_longest_ago_and_a_stale_run_first(
    stores: None,
) -> None:
    """One due job per namespace, the namespace never served (or served longest ago)
    first; within a namespace a stale run before a pending job."""
    from server.core import leases
    from server.knowledge.store import ingestions, pg_jobs

    busy, quiet = unique_org("pgjobs-busy"), unique_org("pgjobs-quiet")
    now = datetime.now(UTC).replace(tzinfo=None)
    old = now - timedelta(minutes=30)
    served = {**_job(busy, source_uri="repo://done.sql", state="ready"), "updated_at": now}
    busy_due = _job(busy, created_at=old, next_attempt_at=old)
    stale = {
        **_job(busy, source_uri="repo://b.sql", state="processing", created_at=old),
        "updated_at": now - timedelta(minutes=9),
    }
    quiet_due = _job(quiet, created_at=now - timedelta(minutes=1), next_attempt_at=now)
    jobs = (served, busy_due, stale, quiet_due)
    held = [await leases.take(f"knowledge-ingest:{j['ingestion_id']}", ttl_s=60) for j in jobs]
    try:
        for row in jobs:
            await pg_jobs.insert(row)
        due = [
            d["ingestion_id"]
            for d in await pg_jobs.due(1000, stale_after_s=120)
            if d["org"] in (busy, quiet)
        ]
        assert due == [quiet_due["ingestion_id"], stale["ingestion_id"]]
    finally:
        for row in jobs:
            taken = await pg_jobs.take(*_key(row))
            assert taken is not None
            await pg_jobs.save(ingestions.ingestion_row(row, state="cancelled"), taken[0])
        for lease in held:
            if lease is not None:
                await lease.release()


async def test_postgres_tells_an_earlier_open_job_of_the_source_and_which_ids_it_holds(
    stores: None,
) -> None:
    from server.knowledge.store import ingestions, pg_jobs

    org = unique_org("pgjobs-earlier")
    now = datetime.now(UTC).replace(tzinfo=None)
    earlier = _job(org, created_at=now - timedelta(minutes=2))
    later = _job(org, created_at=now - timedelta(minutes=1))
    for row in (earlier, later):
        await pg_jobs.insert(row)
    assert await pg_jobs.earlier_open(later)
    assert not await pg_jobs.earlier_open(earlier)
    ids = [earlier["ingestion_id"], str(uuid.uuid4())]
    assert await pg_jobs.held_ids(org, "docs", ids) == {earlier["ingestion_id"]}
    for row in (earlier, later):
        taken = await pg_jobs.take(*_key(row))
        assert taken is not None
        await pg_jobs.save(ingestions.ingestion_row(row, state="cancelled"), taken[0])
    assert not await pg_jobs.earlier_open(later)


async def test_a_user_erasure_keeps_an_original_another_writer_s_open_job_reads(
    stores: None,
) -> None:
    """`_others_owed_originals` on real rows: another writer's open job names its
    original; the erased user's own job and an ended one do not."""
    from server.erasure.store import steps_memory
    from server.knowledge.store import ingestions, pg_jobs

    org = unique_org("pgjobs-others")
    theirs = _job(org, sha256="c" * 64, user_id="bob")
    mine = _job(org, sha256="d" * 64, user_id="alice")
    for row in (theirs, mine):
        await pg_jobs.insert(row)
    pairs = [("docs", "c" * 64), ("docs", "d" * 64)]
    assert await steps_memory._others_owed_originals(org, "alice", pairs) == {("docs", "c" * 64)}
    taken = await pg_jobs.take(*_key(theirs))
    assert taken is not None
    await pg_jobs.save(ingestions.ingestion_row(theirs, state="ready"), taken[0])
    assert await steps_memory._others_owed_originals(org, "alice", pairs) == set()
    await _end(mine)


async def test_a_user_erasure_deletes_the_user_s_jobs_alone(stores: None) -> None:
    from server.erasure.store import steps_pg
    from server.knowledge.store import pg_jobs

    org = unique_org("pgjobs-user")
    mine = _job(org, user_id="alice")
    theirs = _job(org, user_id="bob", source_uri="repo://b.sql")
    for row in (mine, theirs):
        await pg_jobs.insert(row)
    assert await steps_pg.delete_pg_knowledge_by_user(org, "alice") == 1
    assert await pg_jobs.get(*_key(mine)) is None
    assert await pg_jobs.get(*_key(theirs)) == theirs
    residue = await steps_pg.pg_residue(org, "user", "alice")
    assert residue["pg:loom.knowledge_jobs"] == 0
    await _end(mine, theirs)


async def test_a_take_skips_a_job_another_transaction_holds(stores: None) -> None:
    """An erasure holds the rows it deletes until it commits: a take passes over such a
    job at once rather than waiting out its statement timeout."""
    import time

    import asyncpg

    from server.knowledge.store import pg_jobs

    row = _job(unique_org("pgjobs-locked"))
    await pg_jobs.insert(row)
    conn = await asyncpg.connect(os.environ["LOOM_PG_DSN"])
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute(
            "SELECT 1 FROM loom.knowledge_jobs WHERE org = $1 AND namespace = $2"
            " AND ingestion_id = $3 FOR UPDATE",
            *_key(row),
        )
        started = time.monotonic()
        assert await pg_jobs.take(*_key(row)) is None
        assert time.monotonic() - started < 2
        await tx.rollback()
    finally:
        await conn.close()
    assert await pg_jobs.take(*_key(row)) is not None  # free again
    await _end(row)
