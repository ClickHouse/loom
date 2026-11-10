"""The publication pipeline's backlog reads against the real Postgres queues
(`pg_memory.backlog`, `pg_memory.outbox_backlog`, `pg_documents.backlog`).

The queues are shared by every test in the job, so each case backdates its own row
(the oldest age then reaches it whatever else is owed) and counts its parked row by
the drop when it is removed (no worker touches a parked row). Taking a delivery over
advances its `lease_token`, so the write's own flush, still in flight, cannot clear it;
the rows it adds are removed."""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def org_call(control_plane_url: str, mint_test_token: Any) -> Any:
    from server.core import pg

    os.environ.get("LOOM_PG_DSN") or pytest.skip("LOOM_PG_DSN not set")
    org = unique_org("pipeline")
    headers = {
        "Authorization": "Bearer " + mint_test_token(org=org, namespaces=["*"], roles=["writer"])
    }
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60, headers=headers) as c:
        if (await c.get("/v1/health")).json().get("storage_authority") != "postgres":
            pytest.skip("the server under test is not of the Postgres authority")
        assert pg.enabled(), "the pytest process needs LOOM_STORAGE_AUTHORITY=postgres too"

        async def call(op: str, body: dict[str, Any]) -> dict[str, Any]:
            r = await c.post(f"/v1/{op}", json={"org": org, **body})
            assert r.status_code == 200, (op, r.text)
            return dict(r.json())

        yield org, call
    await pg.close_pool()


async def test_the_memory_backlog_counts_pending_and_parked_deliveries(org_call: Any) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    org, call = org_call
    ns = f"pl-{uuid.uuid4().hex[:8]}"
    await call("memory.set", {"namespace": ns, "agent": "a", "memory_key": "k", "value": "a fact"})
    seq = await pg.fetchval(
        "SELECT max(seq) FROM loom.memory_revisions WHERE org = $1 AND namespace = $2", org, ns
    )

    async def owe(lease: str) -> None:
        await pg.execute(
            "INSERT INTO loom.memory_deliveries AS d (org, namespace, seq, targets, lease_until, "
            f"fenced_until) VALUES ($1, $2, $3, $4, {lease}, now()) "
            f"ON CONFLICT (org, namespace, seq) DO UPDATE SET targets = $4, lease_until = {lease}, "
            "fenced_until = now(), lease_token = d.lease_token + 1",
            org,
            ns,
            seq,
            pg_memory.SEARCH_DOC,
        )

    try:
        await owe("now() + interval '1 minute'")
        await pg.execute(
            "UPDATE loom.memory_revisions SET committed_at = committed_at - interval '1 hour' "
            "WHERE org = $1 AND namespace = $2 AND seq = $3",
            org,
            ns,
            seq,
        )
        due = await pg_memory.backlog(1_000_000)
        assert due.pending >= 1 and not due.capped
        assert due.oldest_age_s is not None and due.oldest_age_s >= 3600  # this row's age
        assert (await pg_memory.backlog(0)).capped  # anything owed passes a cap of zero
        await owe("'infinity'")
        parked = (await pg_memory.backlog(1_000_000)).parked
    finally:
        await pg.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )
    assert parked - (await pg_memory.backlog(1_000_000)).parked == 1


async def test_the_outbox_backlog_counts_due_and_parked_rows(org_call: Any) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    org, _call = org_call
    ns, row_hash = f"pl-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    try:
        await pg.execute(
            "INSERT INTO loom.audit_outbox (org, namespace, row_hash, audit, due_at) "
            "VALUES ($1, $2, $3, '{}'::jsonb, now() - interval '1 hour')",
            org,
            ns,
            row_hash,
        )
        due = await pg_memory.outbox_backlog(1_000_000)
        assert due.pending >= 1
        assert due.oldest_age_s is not None and due.oldest_age_s >= 3600  # this row's age
        await pg.execute("UPDATE loom.audit_outbox SET due_at = 'infinity' WHERE org = $1", org)
        parked = (await pg_memory.outbox_backlog(1_000_000)).parked
    finally:
        await pg.execute("DELETE FROM loom.audit_outbox WHERE org = $1", org)
    assert parked - (await pg_memory.outbox_backlog(1_000_000)).parked == 1


async def test_the_knowledge_backlog_counts_pending_and_parked_deliveries(org_call: Any) -> None:
    from server.core import pg
    from server.knowledge.store import pg_documents

    org, call = org_call
    ns = f"pl-{uuid.uuid4().hex[:8]}"
    await call("knowledge.add", {"namespace": ns, "source_uri": "kb://p", "content": "a note"})

    async def owe(lease: str) -> None:
        await pg.execute(
            "INSERT INTO loom.knowledge_deliveries AS d (org, namespace, source_uri, version, "
            "user_id, row, targets, lease_until, fenced_until) "
            f"SELECT org, namespace, source_uri, version, user_id, row, 1, {lease}, now() "
            "FROM loom.knowledge_heads WHERE org = $1 AND namespace = $2 "
            "ON CONFLICT (org, namespace, source_uri, version) DO UPDATE SET settled = false, "
            f"targets = 1, lease_until = {lease}, lease_token = d.lease_token + 1",
            org,
            ns,
        )

    try:
        await owe("now() + interval '1 minute'")
        await pg.execute(
            "UPDATE loom.knowledge_heads SET committed_at = committed_at - interval '1 hour' "
            "WHERE org = $1 AND namespace = $2",
            org,
            ns,
        )
        due = await pg_documents.backlog(1_000_000)
        assert due.pending >= 1
        assert due.oldest_age_s is not None and due.oldest_age_s >= 3600  # this row's age
        assert (await pg_documents.backlog(0)).capped
        await owe("'infinity'")
        parked = (await pg_documents.backlog(1_000_000)).parked
    finally:
        await pg.execute(
            "DELETE FROM loom.knowledge_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )
    assert parked - (await pg_documents.backlog(1_000_000)).parked == 1
