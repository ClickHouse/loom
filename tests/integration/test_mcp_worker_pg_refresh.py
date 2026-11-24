"""Exercise the refresh queue's durable metadata and lease fence in Postgres."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from tests.integration._resources import unique_org
from tests.integration.test_pg_authority import pg_authority as pg_authority
from tests.integration.test_pg_authority import pg_conn as pg_conn

if TYPE_CHECKING:
    from datetime import datetime

    from asyncpg import Connection, Record

    from server.knowledge.domain.mcp_worker_refresh import CompletionReceipt, LiveLease
    from server.knowledge.store.pg_worker_refresh import RefreshEntry


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
               actor, source_class, task, state, fence, claimable_at, receipt
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
    from uuid import UUID

    from server.knowledge.domain.mcp_identity import resource_uri

    scope = entry.task.scope
    row = dict(await _row(conn, entry))
    row["task"] = json.loads(row["task"])
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
        "fence": 0,
        "claimable_at": entry.due,
        "receipt": None,
    }


async def _claim(entry: RefreshEntry, *, namespace: str) -> tuple[LiveLease, ...]:
    from server.knowledge.store import pg_worker_refresh as refresh

    return await refresh.claim(
        org=entry.task.scope.org,
        namespace=namespace,
        worker_id=entry.worker_id,
        limit=1,
        lease_seconds=30,
    )


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
            assert json.loads(staged["receipt"]) == asdict(receipt)
            raise _RollbackCheckpoint


async def _cleanup(conn: Connection, org: str) -> None:
    from server.core import pg

    try:
        async with asyncio.timeout(10), conn.transaction():
            await conn.execute("DELETE FROM loom.knowledge_source_refresh WHERE org = $1", org)
            await conn.execute("DELETE FROM loom.mcp_worker_refresh_admission WHERE org = $1", org)
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

            assert await _claim(first, namespace=second.task.scope.namespace) == ()
            snapshots = []
            leases = []
            for entry in (first, second):
                claimed = await _claim(entry, namespace=entry.task.scope.namespace)
                row = await _row(pg_conn, entry)
                lease = LiveLease(entry.task, str(entry.worker_id), 1, row["claimable_at"])
                assert claimed == (lease,)
                assert (row["state"], row["fence"], row["receipt"]) == ("claimed", 1, None)
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
            assert cancelled == dict(snapshots[0], state="cancelled", fence=lease.fence + 1)
            assert dict(await _row(pg_conn, second)) == dict(snapshots[1])
    finally:
        await _cleanup(pg_conn, org)
