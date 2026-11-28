"""Exercise the refresh queue's durable metadata and lease fence in Postgres."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from tests.integration._resources import unique_org
from tests.integration.test_pg_authority import pg_authority as pg_authority
from tests.integration.test_pg_authority import pg_conn as pg_conn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from asyncpg import Connection, Record

    from server.knowledge.domain.mcp_worker_manual import ManualPosition
    from server.knowledge.domain.mcp_worker_refresh import CompletionReceipt, LiveLease
    from server.knowledge.store.pg_worker_refresh import ClaimPage, RefreshEntry
    from server.knowledge.store.pg_worker_schedules import ScheduleAuthority


class _RollbackCheckpoint(Exception):
    pass


def _entry(org: str, name: str, due: datetime) -> RefreshEntry:
    from server.knowledge.domain.mcp_worker_refresh import DocumentTask, RefreshScope
    from server.knowledge.store.pg_worker_refresh import RefreshEntry

    worker = uuid4()
    scope = RefreshScope(
        org=org,
        namespace=f"refresh-{name}",
        actor=f"synthetic-user-{name}",
        source_id=str(uuid4()),
        connection_id=str(uuid4()),
        destination_id=str(uuid4()),
        connection_generation=1,
        source_generation=2,
        selection_generation=3,
        approval_generation=4,
        profile_digest="a" * 64,
    )
    task = DocumentTask("synthetic-refresh", scope, "synthetic-resource")
    return RefreshEntry(
        task=task,
        worker_id=worker,
        user_id=scope.actor,
        due=due,
        agent="synthetic-worker-refresh",
        session_id="-",
    )


async def _row(conn: Connection, entry: RefreshEntry) -> Record:
    scope = entry.task.scope
    row = await conn.fetchrow(
        """
        SELECT org, namespace, item_key, worker_id, source_uri, agent, user_id, session_id,
               admission_source_id,
               actor, source_class, task, state, attempts, fence, claimable_at, receipt
        FROM loom.knowledge_source_refresh
        WHERE org = $1 AND namespace = $2 AND item_key = $3
        """,
        scope.org,
        scope.namespace,
        entry.task.idempotency_key,
    )
    assert row is not None, (scope.namespace, entry.worker_id, entry.task.idempotency_key)
    return row


async def _assert_pending(conn: Connection, entry: RefreshEntry) -> None:
    from server.knowledge.domain.mcp_identity import resource_uri

    scope = entry.task.scope
    row = dict(await _row(conn, entry))
    assert row == {
        "org": scope.org,
        "namespace": scope.namespace,
        "item_key": entry.task.idempotency_key,
        "worker_id": entry.worker_id,
        "source_uri": resource_uri(scope.source_id, entry.task.resource_id),
        "admission_source_id": UUID(scope.source_id),
        "agent": entry.agent,
        "user_id": entry.user_id,
        "session_id": entry.session_id,
        "actor": scope.actor,
        "source_class": "external",
        "task": {"version": 1, "task": asdict(entry.task)},
        "state": "pending",
        "attempts": 0,
        "fence": 0,
        "claimable_at": entry.due,
        "receipt": None,
    }


async def _claim(entry: RefreshEntry, *, namespace: str) -> ClaimPage:
    from server.knowledge.store import pg_worker_refresh as refresh

    return await refresh.claim(
        org=entry.task.scope.org,
        namespace=namespace,
        worker_id=entry.worker_id,
        limit=1,
        lease_seconds=30,
    )


async def _assert_wrong_org_cannot_claim(conn: Connection, entry: RefreshEntry) -> None:
    from server.knowledge.store import pg_worker_refresh as refresh

    before = dict(await _row(conn, entry))
    page = await refresh.claim(
        org=unique_org("wrong-worker-org"),
        namespace=entry.task.scope.namespace,
        worker_id=entry.worker_id,
        limit=1,
        lease_seconds=30,
    )
    assert not page.leases and page.retired == 0
    assert dict(await _row(conn, entry)) == before


async def _assert_checkpoint_rolls_back(
    pg_conn: Connection, entry: RefreshEntry, lease: LiveLease, receipt: CompletionReceipt
) -> None:
    from server.knowledge.domain.mcp_worker_refresh import RefreshRejected
    from server.knowledge.store import pg_worker_refresh as refresh

    rejection = "requires Postgres authority transaction"
    with pytest.raises(RefreshRejected, match=rejection):
        await refresh.lock_current(pg_conn, lease)
    with pytest.raises(RefreshRejected, match=rejection):
        await refresh.checkpoint(pg_conn, lease, receipt)
    with pytest.raises(_RollbackCheckpoint):
        async with pg_conn.transaction():
            current = await refresh.lock_current(pg_conn, lease)
            assert current == lease
            await refresh.checkpoint(pg_conn, current, receipt)
            staged = await _row(pg_conn, entry)
            assert staged["state"] == "completed"
            assert staged["receipt"] == asdict(receipt)
            raise _RollbackCheckpoint


async def _cleanup(conn: Connection, org: str) -> None:
    from server.core import pg

    try:
        async with asyncio.timeout(10), conn.transaction():
            await conn.execute("DELETE FROM loom.knowledge_source_refresh WHERE org = $1", org)
            await conn.execute("DELETE FROM loom.mcp_worker_refresh_admission WHERE org = $1", org)
            await conn.execute("DELETE FROM loom.mcp_worker_refresh_schedules WHERE org = $1", org)
    finally:
        async with asyncio.timeout(10):
            await pg.close_pool()


@pytest.mark.asyncio
async def test_refresh_claim_rollback_and_cancel_fence(pg_conn: Connection) -> None:
    from server.knowledge.domain.mcp_worker_refresh import (
        CompletionReceipt,
        LiveLease,
        Observation,
        ObservationKind,
        RefreshRejected,
    )
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-refresh")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            first, second = _entry(org, "first", due), _entry(org, "second", due)
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (first,)) == 1
                assert await refresh.enqueue_batch_on(pg_conn, (first,)) == 0
                assert await refresh.enqueue_batch_on(pg_conn, (second,)) == 1
            await _assert_pending(pg_conn, first)
            await _assert_pending(pg_conn, second)

            await _assert_wrong_org_cannot_claim(pg_conn, first)
            assert not (await _claim(first, namespace=second.task.scope.namespace)).leases
            snapshots = []
            leases = []
            for entry in (first, second):
                claimed = await _claim(entry, namespace=entry.task.scope.namespace)
                row = await _row(pg_conn, entry)
                lease = LiveLease(
                    entry.task, str(entry.worker_id), row["fence"], row["claimable_at"]
                )
                assert claimed.leases == (lease,)
                assert claimed.retired == 0
                assert (row["state"], row["receipt"]) == ("claimed", None)
                assert row["fence"] > 0
                snapshots.append(row)
                leases.append(lease)

            lease = leases[0]
            receipt = CompletionReceipt(
                first.task.idempotency_key, Observation(ObservationKind.PRESENT, "c" * 64)
            )
            await _assert_checkpoint_rolls_back(pg_conn, first, lease, receipt)
            assert dict(await _row(pg_conn, first)) == dict(snapshots[0])

            async with pg_conn.transaction():
                assert not await refresh.cancel(pg_conn, first.task, user_id=second.user_id)
                assert await refresh.lock_current(pg_conn, lease) == lease
                assert await refresh.cancel(pg_conn, first.task, user_id=first.user_id)
            rejection = "^refresh lease is no longer current$"
            async with pg_conn.transaction():
                with pytest.raises(RefreshRejected, match=rejection):
                    await refresh.lock_current(pg_conn, lease)
                with pytest.raises(RefreshRejected, match=rejection):
                    await refresh.checkpoint(pg_conn, lease, receipt)
            cancelled = dict(await _row(pg_conn, first))
            assert cancelled == dict(snapshots[0], state="cancelled")
            assert dict(await _row(pg_conn, second)) == dict(snapshots[1])
    finally:
        await _cleanup(pg_conn, org)


def _schedule_authority(entry: RefreshEntry) -> ScheduleAuthority:
    from server.knowledge.domain.mcp_worker_schedule import ScheduleBinding
    from server.knowledge.store import pg_worker_schedules as schedules

    scope = entry.task.scope
    return schedules.ScheduleAuthority(
        ScheduleBinding(
            scope.org,
            scope.namespace,
            scope.source_id,
            scope.connection_id,
            str(entry.worker_id),
            scope.connection_generation,
            scope.source_generation,
            scope.selection_generation,
            scope.approval_generation,
        ),
        scope.profile_digest,
        (entry.task.resource_id,),
        scope.destination_id,
        entry.user_id,
        scope.actor,
        entry.agent,
        entry.session_id,
    )


async def _assert_manual_offer_replay(pg_conn: Connection, entry: RefreshEntry) -> None:
    from server.knowledge.store import pg_worker_schedules as schedules

    authority = _schedule_authority(entry)
    async with pg_conn.transaction():
        offer = await schedules.prepare_manual_on(pg_conn, authority)
        submitted = await schedules.submit_manual_on(pg_conn, authority, offer.position)
    assert submitted.status == "enqueued" and submitted.job_id and submitted.items == 1
    async with pg_conn.transaction():
        retry = await schedules.submit_manual_on(pg_conn, authority, offer.position)
        changed = replace(authority, selected=(*authority.selected, "different-resource"))
        stale = await schedules.submit_manual_on(pg_conn, changed, offer.position)
    assert retry == replace(submitted, status="replayed")
    assert stale.status == "stale" and stale.job_id is None
    assert (
        await pg_conn.fetchval(
            "SELECT count(*) FROM loom.knowledge_source_refresh WHERE org = $1",
            authority.binding.org,
        )
        == 1
    )
    await _assert_expired_offer(pg_conn, authority, offer.position)


async def _assert_expired_offer(
    pg_conn: Connection, authority: ScheduleAuthority, previous: ManualPosition
) -> None:
    from server.knowledge.store import pg_worker_schedules as schedules

    binding = authority.binding
    async with pg_conn.transaction():
        await pg_conn.execute(
            """
            UPDATE loom.mcp_worker_refresh_admission
            SET manual_issued_at = statement_timestamp() - interval '10 minutes',
                manual_expires_at = statement_timestamp() - interval '5 minutes'
            WHERE org = $1 AND namespace = $2 AND source_id = $3
            """,
            binding.org,
            binding.namespace,
            UUID(binding.source_id),
        )
        stale = await schedules.submit_manual_on(pg_conn, authority, previous)
        replacement = await schedules.prepare_manual_on(pg_conn, authority)
        still_busy = await schedules.submit_manual_on(pg_conn, authority, replacement.position)
        again = await schedules.submit_manual_on(pg_conn, authority, previous)
    assert stale.status == again.status == "stale"
    assert replacement.position.sequence == previous.sequence + 1
    assert still_busy.status == "busy"


@pytest.mark.asyncio
async def test_manual_refresh_offer_is_durable_and_single_use(pg_conn: Connection) -> None:
    org = unique_org("worker-pg-manual")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp()")
            await _assert_manual_offer_replay(pg_conn, _entry(org, "manual", due))
    finally:
        await _cleanup(pg_conn, org)


@pytest.mark.asyncio
async def test_reassignment_fences_claimed_and_pending_rows(pg_conn: Connection) -> None:
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-reassignment")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            old = _entry(org, "reassigned", due)
            pending = replace(old, task=replace(old.task, resource_id="another-resource"))
            next_scope = replace(
                old.task.scope, approval_generation=old.task.scope.approval_generation + 1
            )
            new = replace(old, worker_id=uuid4(), task=replace(old.task, scope=next_scope))
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (old, pending)) == 2
            page = await _claim(old, namespace=old.task.scope.namespace)
            (claimed,) = page.leases
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (new,)) == 1
                for previous in (old, pending):
                    assert (await _row(pg_conn, previous))["state"] == "cancelled"
                from server.knowledge.domain.mcp_worker_refresh import RefreshRejected

                with pytest.raises(RefreshRejected, match="no longer current"):
                    await refresh.lock_current(pg_conn, claimed)
            assert not (await _claim(old, namespace=old.task.scope.namespace)).leases
            assert (await _claim(new, namespace=new.task.scope.namespace)).leases[
                0
            ].task == new.task
    finally:
        await _cleanup(pg_conn, org)


@pytest.mark.asyncio
async def test_exhausted_claim_page_is_distinct_from_idle(pg_conn: Connection) -> None:
    from datetime import timedelta

    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-exhaustion")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            first = _entry(org, "exhaustion", due)
            later_scope = replace(first.task.scope, source_id=str(uuid4()))
            later = replace(
                first,
                due=due + timedelta(milliseconds=1),
                task=replace(first.task, scope=later_scope),
            )
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (first,)) == 1
                assert await refresh.enqueue_batch_on(pg_conn, (later,)) == 1
                await pg_conn.execute(
                    "UPDATE loom.knowledge_source_refresh SET state = 'claimed', attempts = 8 "
                    "WHERE org = $1 AND item_key = $2",
                    org,
                    first.task.idempotency_key,
                )
            namespace = first.task.scope.namespace
            retired = await _claim(first, namespace=namespace)
            assert retired.retired == 1 and not retired.leases
            assert (await _row(pg_conn, first))["state"] == "exhausted"
            ready = await _claim(first, namespace=namespace)
            assert ready.retired == 0 and ready.leases[0].task == later.task
            idle = await _claim(first, namespace=namespace)
            assert idle.retired == 0 and not idle.leases
    finally:
        await _cleanup(pg_conn, org)


async def _manual_clock(conn: Connection, sample: datetime) -> None:
    await conn.fetchval(
        "SELECT set_config('loom.refresh_test_clock', $1, true)", sample.isoformat()
    )


@pytest.mark.asyncio
async def test_late_offer_observation_survives_batch_rollback_and_backward_clock(
    pg_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from server.knowledge.store import pg_worker_schedules as schedules

    org = unique_org("worker-pg-manual-clock")
    start = await pg_conn.fetchval("SELECT date_trunc('second', clock_timestamp())")
    authority = _schedule_authority(_entry(org, "clock", start))
    monkeypatch.setattr(
        schedules,
        "_MANUAL_CLOCK",
        schedules._MANUAL_CLOCK.replace(
            "clock_timestamp()", "current_setting('loom.refresh_test_clock')::timestamptz"
        ),
    )
    stage = schedules._enqueue_demand_on

    async def stage_then_expire(conn, current, demand):
        inserted = await stage(conn, current, demand)
        assert inserted == 1
        await _manual_clock(conn, start + timedelta(minutes=5))
        return inserted

    monkeypatch.setattr(schedules, "_enqueue_demand_on", stage_then_expire)
    try:
        async with asyncio.timeout(15):
            async with pg_conn.transaction():
                await _manual_clock(pg_conn, start)
                offer = await schedules.prepare_manual_on(pg_conn, authority)
                result = await schedules.submit_manual_on(pg_conn, authority, offer.position)
                assert result.status == "stale"
            assert (
                await pg_conn.fetchval(
                    "SELECT manual_clock_at FROM loom.mcp_worker_refresh_admission WHERE org = $1",
                    org,
                )
                == offer.expires_at
            )
            async with pg_conn.transaction():
                await _manual_clock(pg_conn, start + timedelta(seconds=1))
                result = await schedules.submit_manual_on(pg_conn, authority, offer.position)
                assert result.status == "stale"
            assert not await pg_conn.fetchval(
                "SELECT count(*) FROM loom.knowledge_source_refresh WHERE org = $1", org
            )
    finally:
        await _cleanup(pg_conn, org)


async def _write_schedule(conn: Connection, entry: RefreshEntry, interval: int) -> None:
    binding = _schedule_authority(entry).binding
    await conn.execute(
        """
        INSERT INTO loom.mcp_worker_refresh_schedules (
            org, namespace, source_id, user_id, agent, session_id, actor, source_class,
            connection_id, worker_id, connection_generation, source_generation,
            selection_generation, worker_generation, interval_seconds, next_due_at, paused
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,'external',$8,$9,$10,$11,$12,$13,$14,$15,false)
        """,
        binding.org,
        binding.namespace,
        UUID(binding.source_id),
        entry.user_id,
        entry.agent,
        entry.session_id,
        entry.task.scope.actor,
        UUID(binding.connection_id),
        UUID(binding.worker_id),
        binding.connection_generation,
        binding.source_generation,
        binding.selection_generation,
        binding.worker_generation,
        interval,
        entry.due,
    )


@pytest.mark.asyncio
async def test_schedule_interval_boundaries_match_postgres(pg_conn: Connection) -> None:
    from asyncpg import CheckViolationError

    from server.knowledge.domain.mcp_worker_schedule import (
        MAX_REFRESH_INTERVAL_SECONDS,
        MIN_REFRESH_INTERVAL_SECONDS,
        PeriodicSchedule,
    )
    from server.knowledge.store import pg_worker_schedules as schedules

    org = unique_org("worker-pg-cadence")
    due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
    try:
        for interval in (MIN_REFRESH_INTERVAL_SECONDS, MAX_REFRESH_INTERVAL_SECONDS):
            entry = _entry(org, f"cadence-{interval}", due)
            authority = _schedule_authority(entry)
            binding = authority.binding
            schedule = PeriodicSchedule(binding, interval, due)
            key = (org, binding.namespace, UUID(binding.source_id))
            await _write_schedule(pg_conn, entry, schedule.interval_seconds)
            async with pg_conn.transaction():
                assert await schedules.enqueue_due_on(pg_conn, authority) == 1
            with pytest.raises(CheckViolationError):
                await pg_conn.execute(
                    "UPDATE loom.mcp_worker_refresh_schedules SET interval_seconds = $4 "
                    "WHERE org = $1 AND namespace = $2 AND source_id = $3",
                    *key,
                    interval - 1 if interval == MIN_REFRESH_INTERVAL_SECONDS else interval + 1,
                )
    finally:
        await _cleanup(pg_conn, org)


def _receipt_for(lease: LiveLease) -> CompletionReceipt:
    from server.knowledge.domain.mcp_worker_refresh import (
        CompletionReceipt,
        Observation,
        ObservationKind,
    )

    return CompletionReceipt(
        lease.task.idempotency_key, Observation(ObservationKind.PRESENT, "a" * 64)
    )


@pytest.mark.asyncio
async def test_pruned_queue_identity_never_reuses_a_claim_fence(pg_conn: Connection) -> None:
    from server.knowledge.domain.mcp_worker_refresh import RefreshRejected
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-fence-incarnation")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            entry = _entry(org, "incarnation", due)
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (entry,)) == 1
            (first,) = (await _claim(entry, namespace=entry.task.scope.namespace)).leases
            async with pg_conn.transaction():
                await refresh.checkpoint(pg_conn, first, _receipt_for(first))
                await pg_conn.execute(
                    "UPDATE loom.knowledge_source_refresh SET updated_at = clock_timestamp() - interval '25 hours' "
                    "WHERE org = $1 AND item_key = $2",
                    org,
                    entry.task.idempotency_key,
                )
                assert await refresh.enqueue_batch_on(pg_conn, (entry,)) == 1
            (second,) = (await _claim(entry, namespace=entry.task.scope.namespace)).leases
            assert second.fence > first.fence
            async with pg_conn.transaction():
                with pytest.raises(RefreshRejected, match="no longer current"):
                    await refresh.lock_current(
                        pg_conn, replace(first, expires_at=second.expires_at)
                    )
                assert await refresh.lock_current(pg_conn, second) == second
    finally:
        await _cleanup(pg_conn, org)


async def _assert_reclaimed_checkpoint(
    conn: Connection, entry: RefreshEntry, first: LiveLease, second: LiveLease
) -> None:
    from server.knowledge.domain.mcp_worker_refresh import RefreshRejected
    from server.knowledge.store import pg_worker_refresh as refresh

    receipt = _receipt_for(second)
    candidates = (replace(second, fence=first.fence), replace(second, expires_at=first.expires_at))
    async with conn.transaction():
        for stale in candidates:
            with pytest.raises(RefreshRejected, match="no longer current"):
                await refresh.checkpoint(conn, stale, receipt)
            row = await _row(conn, entry)
            assert row["state"] == "claimed" and row["receipt"] is None
            assert row["fence"] == second.fence and row["claimable_at"] == second.expires_at
        await refresh.checkpoint(conn, second, receipt)
    row = await _row(conn, entry)
    assert row["state"] == "completed" and row["attempts"] == 2
    assert row["receipt"] == asdict(receipt)


@pytest.mark.asyncio
async def test_expired_claim_reuses_job_but_rejects_stale_checkpoint(pg_conn: Connection) -> None:
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-reclaim")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            entry = _entry(org, "reclaim", due)
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (entry,)) == 1
            (first,) = (
                await refresh.claim(
                    org=org,
                    namespace=entry.task.scope.namespace,
                    worker_id=entry.worker_id,
                    limit=1,
                    lease_seconds=2,
                )
            ).leases
            first_row = await _row(pg_conn, entry)
            assert first_row["attempts"] == 1 and first_row["receipt"] is None
            await pg_conn.fetchval(
                "SELECT pg_sleep(GREATEST(0, extract(epoch FROM ($1::timestamptz - clock_timestamp()))))",
                first.expires_at,
            )
            (second,) = (await _claim(entry, namespace=entry.task.scope.namespace)).leases
            reclaimed = await _row(pg_conn, entry)
            assert reclaimed["attempts"] == 2 and reclaimed["item_key"] == first_row["item_key"]
            assert first.task == second.task == entry.task
            assert second.fence > first.fence and second.expires_at > first.expires_at
            await _assert_reclaimed_checkpoint(pg_conn, entry, first, second)
    finally:
        await _cleanup(pg_conn, org)


async def _contend_after_lock(lease: LiveLease, settle: bool, started: asyncio.Future[int]) -> None:
    from server.core import pg
    from server.knowledge.domain.mcp_worker_refresh import RefreshRejected
    from server.knowledge.store import pg_worker_refresh as refresh

    async with pg.transaction() as contender:
        started.set_result(await contender.fetchval("SELECT pg_backend_pid()"))
        with pytest.raises(RefreshRejected, match="no longer current"):
            if settle:
                await refresh.checkpoint(contender, lease, _receipt_for(lease))
            else:
                await refresh.lock_current(contender, lease)


async def _hold_until_expiry(
    conn: Connection, pending: asyncio.Future[int], deadline: datetime
) -> None:
    pid = await pending
    while not await conn.fetchval("SELECT pg_backend_pid() = ANY(pg_blocking_pids($1))", pid):
        await conn.fetchval("SELECT pg_sleep(0.01)")
    await conn.fetchval(
        "SELECT pg_sleep(GREATEST(0, extract(epoch FROM ($1::timestamptz - clock_timestamp()))))",
        deadline,
    )
    raise _RollbackCheckpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("settle", [False, True])
async def test_lock_wait_cannot_complete_after_lease_deadline(
    pg_conn: Connection, settle: bool
) -> None:
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-lease-clock")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            entry = _entry(org, "deadline", due)
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (entry,)) == 1
            page = await refresh.claim(
                org=org,
                namespace=entry.task.scope.namespace,
                worker_id=entry.worker_id,
                limit=1,
                lease_seconds=3,
            )
            (lease,) = page.leases
            async with asyncio.TaskGroup() as tasks:
                started = asyncio.get_running_loop().create_future()
                with pytest.raises(_RollbackCheckpoint):
                    async with pg_conn.transaction():
                        await refresh.lock_current(pg_conn, lease)
                        tasks.create_task(_contend_after_lock(lease, settle, started))
                        await _hold_until_expiry(pg_conn, started, lease.expires_at)
            row = await _row(pg_conn, entry)
            assert row["state"] == "claimed" and row["receipt"] is None
    finally:
        await _cleanup(pg_conn, org)


async def _complete_cadence_batch(entry: RefreshEntry, *, count: int) -> list[str]:
    from server.core import pg
    from server.knowledge.store import pg_worker_refresh as refresh

    scope = entry.task.scope
    page = await refresh.claim(
        org=scope.org,
        namespace=scope.namespace,
        worker_id=entry.worker_id,
        limit=count,
        lease_seconds=30,
    )
    assert len(page.leases) == count and page.retired == 0
    async with pg.transaction() as conn:
        for lease in page.leases:
            await refresh.checkpoint(conn, lease, _receipt_for(lease))
    return [lease.task.idempotency_key for lease in page.leases]


async def _advance_cadence(conn: Connection, authority: ScheduleAuthority, target: datetime) -> int:
    from server.knowledge.store import pg_worker_schedules as schedules

    async with conn.transaction():
        await _manual_clock(conn, target)
        return await schedules.enqueue_due_on(conn, authority)


async def _schedule_cursor(conn: Connection, org: str) -> datetime:
    return await conn.fetchval(
        "SELECT next_due_at FROM loom.mcp_worker_refresh_schedules WHERE org = $1", org
    )


async def _retained_keys(conn: Connection, org: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT item_key FROM loom.knowledge_source_refresh WHERE org = $1", org
    )
    return {row["item_key"] for row in rows}


async def _put_expired_unmarked(conn: Connection, entry: RefreshEntry) -> str:
    from server.knowledge.store import pg_worker_refresh as refresh

    unrelated = replace(
        entry, task=replace(entry.task, scope=replace(entry.task.scope, source_id=str(uuid4())))
    )
    async with conn.transaction():
        assert await refresh.enqueue_batch_on(conn, (unrelated,)) == 1
    (key,) = await _complete_cadence_batch(unrelated, count=1)
    await conn.execute(
        "UPDATE loom.knowledge_source_refresh SET admission_source_id = NULL, "
        "updated_at = clock_timestamp() - interval '24 hours' WHERE org = $1 AND item_key = $2",
        entry.task.scope.org,
        key,
    )
    return key


@pytest.mark.asyncio
async def test_large_selection_cadence_retains_one_day_over_three_cycles(
    pg_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from server.knowledge.domain.mcp_worker_schedule import minimum_interval_for_selected
    from server.knowledge.store import pg_worker_schedules as schedules

    monkeypatch.setattr(
        schedules,
        "_SCHEDULE_CLOCK",
        "SELECT current_setting('loom.refresh_test_clock')::timestamptz",
    )
    org = unique_org("worker-pg-cadence-repeated")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '3 days'")
            entry = _entry(org, "full-batch", due)
            selection = tuple(f"urn:synthetic:resource:{i}" for i in range(64))
            authority = replace(_schedule_authority(entry), selected=selection)
            interval = minimum_interval_for_selected(len(selection))
            await _write_schedule(pg_conn, entry, interval)
            unmarked = await _put_expired_unmarked(pg_conn, entry)
            await _assert_cadence_cycles(
                pg_conn, entry, authority, due, timedelta(seconds=interval), unmarked
            )
    finally:
        await _cleanup(pg_conn, org)


async def _assert_cadence_cycles(conn, entry, authority, due, interval, unmarked):
    org = entry.task.scope.org
    assert await _advance_cadence(conn, authority, due) == 64
    first = await _complete_cadence_batch(entry, count=64)
    assert await _schedule_cursor(conn, org) == due + interval
    assert await _advance_cadence(conn, authority, due + interval) == 64
    second = await _complete_cadence_batch(entry, count=64)
    assert await _retained_keys(conn, org) == {unmarked, *first, *second}
    third_due = due + interval * 2
    assert await _schedule_cursor(conn, org) == third_due
    assert await _advance_cadence(conn, authority, third_due) == 0
    assert await _schedule_cursor(conn, org) == third_due
    await conn.execute(
        "UPDATE loom.knowledge_source_refresh SET updated_at = clock_timestamp() - interval '24 hours' "
        "WHERE org = $1 AND item_key = ANY($2::text[])",
        org,
        first,
    )
    assert await _advance_cadence(conn, authority, third_due) == 64
    third = await _complete_cadence_batch(entry, count=64)
    assert await _schedule_cursor(conn, org) == third_due + interval
    assert await _retained_keys(conn, org) == {unmarked, *second, *third}


@pytest.mark.asyncio
async def test_manual_offer_forward_step_does_not_park_queue_after_correction(
    pg_conn: Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    from server.knowledge.store import pg_worker_schedules as schedules

    monkeypatch.setattr(
        schedules,
        "_MANUAL_CLOCK",
        schedules._MANUAL_CLOCK.replace(
            "clock_timestamp()", "current_setting('loom.refresh_test_clock')::timestamptz"
        ),
    )
    org = unique_org("worker-pg-manual-compensated")
    try:
        async with asyncio.timeout(15):
            wall = await pg_conn.fetchval("SELECT clock_timestamp()")
            entry = _entry(org, "compensated", wall)
            authority = _schedule_authority(entry)
            async with pg_conn.transaction():
                await _manual_clock(pg_conn, wall + timedelta(minutes=2))
                offer = await schedules.prepare_manual_on(pg_conn, authority)
            async with pg_conn.transaction():
                await _manual_clock(pg_conn, wall)
                result = await schedules.submit_manual_on(pg_conn, authority, offer.position)
                assert result.status == "enqueued"
            (lease,) = (await _claim(entry, namespace=entry.task.scope.namespace)).leases
            assert lease.task.job_id == result.job_id
    finally:
        await _cleanup(pg_conn, org)


async def _assert_terminal_replay(conn: Connection, entry: RefreshEntry, lease: LiveLease) -> None:
    from server.knowledge.domain.mcp_worker_refresh import (
        Observation,
        ObservationKind,
        RefreshRejected,
    )
    from server.knowledge.store import pg_worker_refresh as refresh

    async def current_authority(_conn: Connection, candidate: LiveLease):
        assert candidate == lease
        return refresh.CompletionAuthority(entry.task, str(entry.worker_id), entry.user_id)

    async def different_owner(_conn: Connection, candidate: LiveLease):
        return refresh.CompletionAuthority(candidate.task, candidate.worker_id, "different-owner")

    receipt = _receipt_for(lease)
    completion = refresh.WorkerCompletion(lease, receipt, current_authority)
    before = dict(await _row(conn, entry))
    await conn.fetchval(
        "SELECT pg_sleep(GREATEST(0, extract(epoch FROM ($1::timestamptz - clock_timestamp()))))",
        lease.expires_at,
    )
    async with conn.transaction():
        assert await refresh.replay_completion(conn, completion)
        assert not await refresh.replay_completion(
            conn, replace(completion, authorize=different_owner)
        )
        for wrong in (replace(lease, fence=lease.fence + 1), replace(lease, expires_at=entry.due)):
            assert not await refresh.replay_completion(conn, replace(completion, lease=wrong))
        wrong_receipt = replace(receipt, observation=Observation(ObservationKind.PRESENT, "b" * 64))
        with pytest.raises(RefreshRejected, match="receipt conflicts"):
            await refresh.replay_completion(conn, replace(completion, receipt=wrong_receipt))
    assert dict(await _row(conn, entry)) == before
    await conn.execute(
        "UPDATE loom.knowledge_source_refresh SET updated_at = clock_timestamp() - interval '25 hours' "
        "WHERE org = $1 AND item_key = $2",
        entry.task.scope.org,
        entry.task.idempotency_key,
    )
    async with conn.transaction():
        assert not await refresh.replay_completion(conn, completion)
    assert (await _row(conn, entry))["state"] == "completed"


@pytest.mark.asyncio
async def test_committed_receipt_replays_only_for_exact_current_authority(
    pg_conn: Connection,
) -> None:
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-terminal-receipt")
    try:
        async with asyncio.timeout(15):
            due = await pg_conn.fetchval("SELECT clock_timestamp() - interval '1 second'")
            entry = _entry(org, "terminal-receipt", due)
            async with pg_conn.transaction():
                assert await refresh.enqueue_batch_on(pg_conn, (entry,)) == 1
            page = await refresh.claim(
                org=org,
                namespace=entry.task.scope.namespace,
                worker_id=entry.worker_id,
                limit=1,
                lease_seconds=3,
            )
            (lease,) = page.leases
            async with pg_conn.transaction():
                await refresh.checkpoint(pg_conn, lease, _receipt_for(lease))
            await _assert_terminal_replay(pg_conn, entry, lease)
    finally:
        await _cleanup(pg_conn, org)


async def _refresh_rows_for_owner(conn: Connection, org: str, owner: str) -> tuple[int, ...]:
    row = await conn.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM loom.knowledge_source_refresh WHERE org = $1 AND user_id = $2),
            (SELECT count(*) FROM loom.mcp_worker_refresh_schedules WHERE org = $1 AND user_id = $2),
            (SELECT count(*) FROM loom.mcp_worker_refresh_admission WHERE org = $1 AND user_id = $2)
        """,
        org,
        owner,
    )
    assert row is not None
    return tuple(row)


async def _wait_for_refresh_erasure_lock(conn: Connection, erase: asyncio.Task[int]) -> None:
    while not await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_locks held JOIN pg_locks waiting
              USING (locktype, database, classid, objid, objsubid)
            WHERE held.pid = pg_backend_pid() AND held.locktype = 'advisory' AND held.granted
              AND NOT waiting.granted AND waiting.mode = 'ExclusiveLock'
        )
        """
    ):
        assert not erase.done(), "user erasure did not discover the refresh namespace lock"
        await asyncio.sleep(0.01)


async def _erase_refresh_owner(conn: Connection, entry: RefreshEntry) -> None:
    from server.core import pg
    from server.erasure.store.steps_pg import delete_pg_knowledge_by_user

    scope = entry.task.scope
    async with asyncio.TaskGroup() as tasks:
        async with conn.transaction():
            await conn.execute(
                f"SELECT pg_advisory_xact_lock_shared({pg.KNOWLEDGE_LOCK_KEY})",
                scope.org,
                scope.namespace,
            )
            erase = tasks.create_task(delete_pg_knowledge_by_user(scope.org, entry.user_id))
            await _wait_for_refresh_erasure_lock(conn, erase)
    assert await _refresh_rows_for_owner(conn, scope.org, entry.user_id) == (0, 0, 0)


@pytest.fixture
async def connected_clickhouse() -> AsyncIterator[None]:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    try:
        yield
    finally:
        await ch.disconnect()


@pytest.mark.asyncio
@pytest.mark.usefixtures("connected_clickhouse")
async def test_user_erasure_discovers_and_removes_refresh_only_metadata(
    pg_conn: Connection,
) -> None:
    from server.knowledge.store import pg_worker_refresh as refresh

    org = unique_org("worker-pg-refresh-erasure")
    try:
        async with asyncio.timeout(20):
            now = await pg_conn.fetchval("SELECT clock_timestamp()")
            first, other = _entry(org, "erased", now), _entry(org, "retained", now)
            neighbor = replace(other.task.scope, namespace=first.task.scope.namespace)
            other = replace(other, task=replace(other.task, scope=neighbor))
            async with pg_conn.transaction():
                for entry in (first, other):
                    await _write_schedule(pg_conn, entry, 720)
                    assert await refresh.enqueue_batch_on(pg_conn, (entry,)) == 1
                    assert await _refresh_rows_for_owner(pg_conn, org, entry.user_id) == (1, 1, 1)
            await _erase_refresh_owner(pg_conn, first)
            assert await _refresh_rows_for_owner(pg_conn, org, other.user_id) == (1, 1, 1)
            assert (await _row(pg_conn, other))["state"] == "pending"
    finally:
        await _cleanup(pg_conn, org)
