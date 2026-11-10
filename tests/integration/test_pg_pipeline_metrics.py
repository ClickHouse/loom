"""The publication pipeline's backlog reads against the real Postgres queues
(`pg_memory.backlog`, `pg_memory.outbox_backlog`, `pg_documents.backlog`).

The queues are shared by every test in the job, so each case counts at least its own
rows and checks the cap and the parked count on them; the rows it adds are removed."""

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
            "fenced_until = now()",
            org,
            ns,
            seq,
            pg_memory.SEARCH_DOC,
        )

    try:
        await owe("now() + interval '1 minute'")
        due = await pg_memory.backlog(1_000_000)
        assert due.pending >= 1 and due.oldest_age_s is not None and not due.capped
        assert (await pg_memory.backlog(1)).capped  # one counted reaches a cap of one
        await owe("'infinity'")
        parked = await pg_memory.backlog(1_000_000)
        assert parked.parked >= 1
    finally:
        await pg.execute(
            "DELETE FROM loom.memory_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )


async def test_the_outbox_backlog_counts_due_and_parked_rows(org_call: Any) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    org, _call = org_call
    ns, row_hash = f"pl-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    try:
        await pg.execute(
            "INSERT INTO loom.audit_outbox (org, namespace, row_hash, audit, due_at) "
            "VALUES ($1, $2, $3, '{}'::jsonb, now() - interval '30 seconds')",
            org,
            ns,
            row_hash,
        )
        due = await pg_memory.outbox_backlog(1_000_000)
        assert due.pending >= 1 and due.oldest_age_s is not None and due.oldest_age_s >= 0
        await pg.execute("UPDATE loom.audit_outbox SET due_at = 'infinity' WHERE org = $1", org)
        assert (await pg_memory.outbox_backlog(1_000_000)).parked >= 1
    finally:
        await pg.execute("DELETE FROM loom.audit_outbox WHERE org = $1", org)


async def test_the_knowledge_backlog_counts_pending_deliveries(org_call: Any) -> None:
    from server.core import pg
    from server.knowledge.store import pg_documents

    org, call = org_call
    ns = f"pl-{uuid.uuid4().hex[:8]}"
    await call("knowledge.add", {"namespace": ns, "source_uri": "kb://p", "content": "a note"})
    try:
        await pg.execute(
            "INSERT INTO loom.knowledge_deliveries AS d (org, namespace, source_uri, version, "
            "user_id, row, targets, lease_until, fenced_until) "
            "SELECT org, namespace, source_uri, version, user_id, row, 1, "
            "now() + interval '1 minute', now() FROM loom.knowledge_heads "
            "WHERE org = $1 AND namespace = $2 "
            "ON CONFLICT (org, namespace, source_uri, version) DO UPDATE SET settled = false, "
            "targets = 1, lease_until = now() + interval '1 minute'",
            org,
            ns,
        )
        due = await pg_documents.backlog(1_000_000)
        assert due.pending >= 1 and due.oldest_age_s is not None
        assert (await pg_documents.backlog(1)).capped
    finally:
        await pg.execute(
            "DELETE FROM loom.knowledge_deliveries WHERE org = $1 AND namespace = $2", org, ns
        )
