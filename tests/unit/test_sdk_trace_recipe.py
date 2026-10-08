"""The SDK writes a trace as its actor and reads traces
through their facts."""

from __future__ import annotations

from typing import Any

import pytest
from loom import Agent


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, op_path: str, payload: dict[str, Any], idempotency_key=None, *, timeout_s=None
    ):
        self.calls.append((op_path, payload))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-7"}
        if op_path == "/v1/run.complete":
            return {"run_id": payload["run_id"], "status": payload["status"]}
        if op_path == "/v1/memory.set_from_messages":
            return {"written": [{"memory_key": "eric_day_2_dinner", "version": 1}], "queue_id": ""}
        if op_path == "/v1/memory.search":
            return {"results": [], "embedding_model": "x"}
        return {}

    async def close(self) -> None:  # pragma: no cover
        pass

    def payload(self, op: str) -> dict[str, Any]:
        return [p for o, p in self.calls if o == op][-1]


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x")
    a.transport = _FakeTransport()  # type: ignore[assignment]
    return a


@pytest.mark.asyncio
async def test_trace_writes_under_the_actor_and_the_run(agent: Agent) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="plan") as run:
        await run.trace("Eric", [{"role": "user", "content": "I am Eric. …"}])
    p = t.payload("/v1/memory.set_from_messages")
    assert p["user_id"] == "eric" and p["run_id"] == "run-7"
    assert p["messages"][0]["content"].startswith("I am Eric")


@pytest.mark.asyncio
async def test_read_traces_asks_for_source_bodies_with_a_trace_sized_budget(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.memory.read_traces("join Eric's dinner on day 2")
    p = t.payload("/v1/memory.search")
    assert p["bodies"] == "source" and p["body_budget_tokens"] == 32000 and p["top_k"] == 25
    await agent.memory.search("x", bodies="unit", body_budget_tokens=4000)
    p = t.payload("/v1/memory.search")
    assert p["bodies"] == "unit" and p["body_budget_tokens"] == 4000
    await agent.memory.search("x")
    assert "bodies" not in t.payload("/v1/memory.search")
