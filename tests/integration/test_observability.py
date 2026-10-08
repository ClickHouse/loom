"""agent_events emission + the cost roll-up — end-to-end.

Every Loom operation writes one row to `loom.agent_events`. The refreshable
view `loom.agent_cost_rollup_refresh` folds them once a minute into the
AggregatingMergeTree `loom.agent_cost_rollup_daily` by (org, namespace, agent,
day, operation), leaving the newest ten seconds to the next refresh.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


async def _wait_for_events(
    ch_client: Any, namespace: str, expected: int, timeout_s: float = 3.0
) -> int:
    """Poll until `expected` events for the namespace are visible (events
    flush every 100ms; allow a bit of slack)."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        result = ch_client.query(
            "SELECT count() FROM loom.agent_events WHERE namespace = {ns:String}",
            parameters={"ns": namespace},
        )
        last = int(result.result_rows[0][0])
        if last >= expected:
            return last
        await asyncio.sleep(0.1)
    return last


@pytest.mark.asyncio
async def test_each_operation_emits_one_event(agent, ch_client) -> None:
    """memory.set + memory.get + memory.delete + memory.list = 4 events."""
    await agent.memory.set("k1", 1, memory_type="semantic")
    await agent.memory.get("k1")
    await agent.memory.delete("k1")
    await agent.memory.list()

    count = await _wait_for_events(ch_client, agent.namespace, expected=4)
    assert count >= 4, f"expected at least 4 events; got {count}"


@pytest.mark.asyncio
async def test_agent_events_records_operation_name(agent, ch_client) -> None:
    """The `operation` column captures the dotted op name."""
    await agent.memory.set("trace_me", 1, memory_type="semantic")
    await _wait_for_events(ch_client, agent.namespace, expected=1)

    result = ch_client.query(
        "SELECT operation, success FROM loom.agent_events WHERE namespace = {ns:String}",
        parameters={"ns": agent.namespace},
    )
    rows = list(result.result_rows)
    assert any(op == "memory.set" and success for op, success in rows)


@pytest.mark.asyncio
async def test_failed_operation_is_recorded_with_success_false(agent, ch_client) -> None:
    """Validation errors emit an event with success=false."""
    import httpx

    # Use a unique org so this test's failed event is unambiguous to find.
    marker_org = f"marker-{agent.namespace}"

    async with httpx.AsyncClient(base_url=agent.transport._base_url) as client:
        resp = await client.post("/v1/memory.set", json={"org": marker_org})
        assert resp.status_code == 422  # validation failed

    # Poll for the failed event (events flush every 100ms).
    deadline = asyncio.get_event_loop().time() + 3.0
    failed_count = 0
    while asyncio.get_event_loop().time() < deadline:
        result = ch_client.query(
            """
            SELECT count() FROM loom.agent_events
            WHERE org = {org:String} AND success = false AND error_code != ''
            """,
            parameters={"org": marker_org},
        )
        failed_count = int(result.result_rows[0][0])
        if failed_count >= 1:
            break
        await asyncio.sleep(0.1)
    assert failed_count >= 1, f"expected at least one failed-event row for org={marker_org}"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_cost_rollup_refresh_folds_events(agent, ch_client) -> None:
    """The refresh keeps AggregateFunction state; queries finalize via *Merge.
    Events younger than the ten-second settle wait for a later refresh, so the
    test re-triggers the view until the three writes are folded."""
    await agent.memory.set("k1", 1, memory_type="semantic")
    await agent.memory.set("k2", 2, memory_type="semantic")
    await agent.memory.set("k3", 3, memory_type="semantic")
    await _wait_for_events(ch_client, agent.namespace, expected=3)

    deadline = asyncio.get_event_loop().time() + 45.0
    ops_count = 0
    while asyncio.get_event_loop().time() < deadline:
        ch_client.command("SYSTEM REFRESH VIEW loom.agent_cost_rollup_refresh")
        ch_client.command("SYSTEM WAIT VIEW loom.agent_cost_rollup_refresh")
        result = ch_client.query(
            """
            SELECT countMerge(ops_state) AS ops
            FROM loom.agent_cost_rollup_daily
            WHERE namespace = {ns:String} AND operation = 'memory.set'
            """,
            parameters={"ns": agent.namespace},
        )
        ops_count = int(result.result_rows[0][0])
        if ops_count >= 3:
            break
        await asyncio.sleep(2.0)
    assert ops_count >= 3, f"cost roll-up expected at least 3 memory.set ops; got {ops_count}"
