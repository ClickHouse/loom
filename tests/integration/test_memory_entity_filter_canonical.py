"""`memory.set(entities=["Billing Services"])` then `memory.search(entity_filter=["billing
service"])` boosts the row: both sides meet in the canonical entity form. In-process
against a live ClickHouse; skips without LOOM_CH_HOST."""

from __future__ import annotations

import os

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("entcanon")


async def test_a_plural_entity_meets_its_singular_filter(ch_client) -> None:
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    try:
        await get_client().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clickhouse unavailable: {e}")
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=ns,
            agent="a",
            memory_key="billing-owner",
            value="the invoices team owns the billing services rollout",
            entities=["Billing Services"],
        )
    )
    out = await handle_memory_search(
        MemorySearchInput(
            org=ORG,
            namespace=ns,
            agent="*",
            query="who owns the rollout",
            entity_filter=["billing service"],
        )
    )
    (hit,) = [h for h in out.results if h.memory_key == "billing-owner"]
    assert hit.score_components["entity_boost_sql"] == 0.5  # every filter entity matched
    stored = ch_client.query(
        "SELECT entities FROM loom.memory FINAL WHERE org = {org:String} "
        "AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    assert stored == [(["billing service"],)]
