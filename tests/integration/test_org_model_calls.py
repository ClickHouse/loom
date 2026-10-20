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
    customer._reset_for_tests(read=False)
    yield client
    customer._reset_for_tests(read=False)
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


async def test_the_work_queries_run_with_a_switched_off_org_left_out(ch: Any) -> None:
    from server.memory.app import extraction_queue
    from server.memory.store import entities, sweeps

    org = unique_org("ai-off")
    excluded = frozenset({org, "another"})
    stubs = await sweeps.find_embedding_stubs(batch_size=5, max_attempts=3, exclude_orgs=excluded)
    keys = await entities.find_unentitied_keys(batch_size=5, exclude_orgs=excluded)
    claimed = await extraction_queue.claim_batch(
        limit=5, lease_seconds=60, org_filter=org, exclude_orgs=excluded
    )
    assert all(r["org"] not in excluded for r in [*stubs, *keys, *claimed])
