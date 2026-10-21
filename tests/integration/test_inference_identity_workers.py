"""Identity holds preserve queue compatibility and leave room for other writers."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

import server.memory.store as memory_repo
from server.core.ch import get_client
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_bulk import MemorySetFromMessagesInput
from server.memory.app import extraction_queue as queue
from tests.integration._resources import unique_org


@pytest.fixture(autouse=True)
async def _connected(control_plane_url: str):
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def test_an_older_worker_reclaims_a_hold_when_its_lease_ends(
    monkeypatch, control_plane_url, mint_test_token
) -> None:
    ch = get_client()
    org = unique_org("held-queue")
    await queue.enqueue(
        MemorySetFromMessagesInput(
            org=org,
            namespace="mem",
            agent="writer",
            messages=[{"role": "user", "content": "Prefer short replies"}],
        ),
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    claimed = await queue.claim_batch(limit=1, lease_seconds=60, org_filter=org, ch=ch)
    await queue.mark_held(claimed[0], reason="refused", seconds=60, ch=ch)
    old_selection = """
        SELECT queue_id FROM loom.memory_extraction_queue FINAL
        WHERE org = {org:String}
          AND (status = 'pending' OR (status = 'running' AND lease_until < now64(3)))
    """
    assert await ch.query_all(old_selection, params={"org": org}) == []
    status = await queue.list_status(
        org=org, namespace="mem", status="held", scope=WHOLE_NAMESPACE, ch=ch
    )
    assert len(status) == 1 and status[0]["status"] == "held"
    assert await queue.pending_count(org=org, namespace="mem", ch=ch) == 1
    token = mint_test_token(org=org, namespaces=["mem"], roles=["writer"])
    async with httpx.AsyncClient(base_url=control_plane_url) as client:
        response = await client.post(
            "/v1/memory.extraction_status",
            headers={"Authorization": f"Bearer {token}"},
            json={"org": org, "namespace": "mem", "status": "held"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["pending"] == 1
    monkeypatch.setattr(queue, "_now", lambda: datetime.now(UTC) - timedelta(seconds=1))
    await queue.mark_held(claimed[0], reason="refused", seconds=0, ch=ch)
    assert len(await ch.query_all(old_selection, params={"org": org})) == 1


async def test_held_stubs_are_excluded_before_the_candidate_limit() -> None:
    ch = get_client()
    org = unique_org("held-stubs")
    now = datetime.now(UTC)
    rows = [
        memory_repo.build_embedding_row(
            org=org,
            namespace="shared",
            agent="label",
            user_id="-",
            session_id="-",
            memory_key=f"held-{i}",
            content_excerpt="held",
            embedding_model="fake-ngram-1024",
            embedding_dim=0,
            embed_retry_at=now + timedelta(minutes=10),
            created_at=now - timedelta(minutes=2),
            version=i + 2,
        )
        for i in range(12)
    ]
    rows.append(
        memory_repo.build_embedding_row(
            org=org,
            namespace="shared",
            agent="label",
            user_id="-",
            session_id="-",
            memory_key="healthy",
            content_excerpt="healthy",
            embedding_model="fake-ngram-1024",
            embedding_dim=0,
            created_at=now - timedelta(minutes=1),
            version=1,
        )
    )
    await ch.insert("loom.memory_embeddings", rows)
    found = await memory_repo.find_embedding_stubs(
        batch_size=2, max_attempts=5, org_filter=org, ch=ch
    )
    assert [r["memory_key"] for r in found] == ["healthy"]
