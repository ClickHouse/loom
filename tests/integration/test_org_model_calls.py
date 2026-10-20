"""An org's model-call switch against a real ClickHouse: `org.set_model_calls`
writes it, `org.model_calls` reads it back, every process's copy follows it,
and the queries the embedding worker, the extraction queue and the entity
extractor pick work with leave the switched-off org out."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.ch import get_client, own_write_reads
from server.identity.api.models_model_calls import OrgModelCallsInput, OrgSetModelCallsInput
from server.identity.app import model_calls
from server.llm import customer
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def ch() -> Any:
    client = get_client()
    await client.connect()
    customer._switch._reset_for_tests(None, read_age_s=None, due_in_s=0.0)
    yield client
    customer._switch._reset_for_tests(None, read_age_s=None, due_in_s=0.0)
    await client.disconnect()


async def test_the_switch_is_written_read_back_and_followed(ch: Any) -> None:
    org, other = unique_org("ai-off"), unique_org("ai-on")
    before = await model_calls.handle_org_model_calls(OrgModelCallsInput(org=org))
    assert before.enabled is True and before.updated_by == ""

    await model_calls.handle_org_set_model_calls(
        OrgSetModelCallsInput(org=org, enabled=False, reason="AI off")
    )
    with own_write_reads():
        row = await model_calls.handle_org_model_calls(OrgModelCallsInput(org=org))
        off = await customer.switched_off(org)
        on = await customer.switched_off(other)
    assert (row.enabled, row.reason, row.updated_by) == (False, "AI off", "admin")
    assert row.updated_at is not None
    assert off is True and on is False

    await model_calls.handle_org_set_model_calls(OrgSetModelCallsInput(org=org, enabled=True))
    with own_write_reads():
        row = await model_calls.handle_org_model_calls(OrgModelCallsInput(org=org))
        off = await customer.switched_off(org)
    assert row.enabled is True and row.reason == "" and off is False


async def test_the_work_queries_return_only_the_org_left_switched_on(ch: Any) -> None:
    from datetime import UTC, datetime

    from server.memory.app import extraction_queue
    from server.memory.store import entities, sweeps

    off, on = unique_org("ai-off"), unique_org("ai-on")
    now = datetime.now(UTC).replace(tzinfo=None)
    sync = {"async_insert": 0, "wait_for_async_insert": 1}
    ident = {"namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    from server.memory.store import rows as row_builders

    await ch.insert(
        "loom.memory_embeddings",
        [
            row_builders.build_embedding_row(
                **ident,
                org=org,
                scope="private",
                embedding_model="fake-ngram-1024",
                memory_key="k",
                memory_type="semantic",
                content_excerpt="text",
                embedding_dim=0,
                created_at=now,
                version=1,
            )
            for org in (off, on)
        ],
        settings=sync,
    )
    await ch.insert(
        "loom.memory",
        [
            {
                **ident,
                "org": org,
                "memory_key": "k",
                "memory_type": "semantic",
                "value": "a fact",
                "entities": [],
                "sensitivity": "low",
                "created_at": now,
                "updated_at": now,
                "version": 1,
            }
            for org in (off, on)
        ],
        settings=sync,
    )
    await ch.insert(
        extraction_queue.TABLE,
        [
            {
                "org": org,
                "namespace": "n",
                "queue_id": f"q-{org}",
                "status": extraction_queue.STATUS_PENDING,
                "enqueued_at": now,
                "lease_until": now,
            }
            for org in (off, on)
        ],
        settings=sync,
    )
    excluded = frozenset({off})
    with own_write_reads():
        stubs = await sweeps.find_embedding_stubs(
            batch_size=50, max_attempts=3, exclude_orgs=excluded
        )
        keys = await entities.find_unentitied_keys(batch_size=500, exclude_orgs=excluded)
        claimed = await extraction_queue.claim_batch(
            limit=500, lease_seconds=60, exclude_orgs=excluded
        )
    for found in (stubs, keys, claimed):
        orgs = {r["org"] for r in found}
        assert on in orgs and off not in orgs
