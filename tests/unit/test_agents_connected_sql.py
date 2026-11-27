"""agents.connected counts an agent's own calls. A worker's ledger row can name the
agent whose request it replayed, hours after that agent's last call: it is neither a
connection nor an op of that agent."""

from __future__ import annotations

import uuid

import pytest

from server.insights.store import agents
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl


def _event(agent: str, operation: str, kind: str) -> str:
    # A request id of its own: the table drops a repeat of an identical insert.
    return (
        "INSERT INTO loom.agent_events (org, namespace, agent, operation, kind, "
        f"upstream_cost_usd, request_id, ts) VALUES ('o', 'ns', '{agent}', '{operation}', "
        f"'{kind}', 0.25, '{uuid.uuid4()}', now())"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_workers_row_for_an_agent_is_not_that_agents_connection() -> None:
    ch = LocalCH(
        ddl("agent_events"),
        ddl("agent_events_private"),
        _event("ci-bot", "worker.extraction_queue", "worker"),
        _event("desk", "memory.search", "request"),
        _event("desk", "worker.extraction_queue", "worker"),
    )
    where, params = agents.connected_where("o", 1, namespace="ns", scope_where="", scope_params={})

    rows = await agents.connected_rows(where, params, lookback_days=30, ch=ch)

    assert [(r["agent"], int(r["ops"]), float(r["cost_usd"])) for r in rows] == [("desk", 1, 0.25)]
    assert await agents.new_agent_count(where, params, lookback_days=30, ch=ch) == 1
