"""A walk is remembered; a badge asks for a number.

A `facts.traverse` walk reads tens of MB of entity_triples and the Graph
re-walks the same seed on every hover, so a walk is remembered for a minute
per seed and arguments. The sidebar counts the agents first seen today with
`agents.connected(count_only)` — one aggregate, no rows — every five minutes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.insights.api.models_agents import AgentsConnectedInput
from server.insights.app import agents as agents_handler
from server.memory.api.models_facts import FactsTraverseInput, FactsTraverseOutput
from server.memory.app import facts as facts_handler
from server.memory.app import graph_write_marks
from server.transport import mcp_schemas


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    facts_handler.reset_traverse_cache()
    agents_handler.reset_connected_cache()

    async def _never_written(org: str, ns: str, *, wait_s: float) -> tuple[None, datetime]:
        return None, datetime.now(UTC)

    monkeypatch.setattr(graph_write_marks, "last_write", _never_written)


@pytest.mark.asyncio
async def test_the_same_walk_within_a_minute_is_answered_from_the_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    walks = 0

    async def fake_walk(inp: FactsTraverseInput) -> FactsTraverseOutput:
        nonlocal walks
        walks += 1
        return FactsTraverseOutput(seed=inp.seed_entity, nodes=[], truncated=False)

    monkeypatch.setattr(facts_handler, "_handle_facts_traverse_uncached", fake_walk)
    a = FactsTraverseInput(org="o", namespace="n", seed_entity="Exporter", max_hops=2)
    out1 = await facts_handler.handle_facts_traverse(a)
    out2 = await facts_handler.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity=" exporter ", max_hops=2)
    )
    assert walks == 1 and out1.seed == out2.seed
    out1.nodes.append("poison")  # type: ignore[arg-type]
    out3 = await facts_handler.handle_facts_traverse(a)
    assert out3.nodes == [] and walks == 1  # the memo hands out copies
    await facts_handler.handle_facts_traverse(
        FactsTraverseInput(org="o", namespace="n", seed_entity="exporter", max_hops=3)
    )
    assert walks == 2  # other arguments, another walk


class _CH:
    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sqls.append(sql)
        return {"n": 3}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list:
        self.sqls.append(sql)
        raise AssertionError("count_only must not list rows")


@pytest.mark.asyncio
async def test_count_only_is_one_aggregate_and_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH()
    monkeypatch.setattr(agents_handler, "get_client", lambda: ch)
    out = await agents_handler.handle_agents_connected(
        AgentsConnectedInput(org="o", namespace="n", window_hours=24, count_only=True)
    )
    assert out.new_count == 3 and out.rows == [] and out.window_hours == 24
    assert len(ch.sqls) == 1
    sql = ch.sqls[0]
    assert "count() AS n" in sql and "GROUP BY agent" in sql
    assert "first_seen > now() - toIntervalHour({wh:UInt32}) AND ops > 0" in sql
    assert "LEFT JOIN" not in sql and "namespaces_touched" not in sql
    # remembered for a minute, apart from the rows listing
    again = await agents_handler.handle_agents_connected(
        AgentsConnectedInput(org="o", namespace="n", window_hours=24, count_only=True)
    )
    assert again.new_count == 3 and len(ch.sqls) == 1


def test_count_only_is_hidden_from_the_mcp_listing() -> None:
    assert "count_only" in mcp_schemas._HIDDEN_OP_PROPS["agents.connected"]
