"""`MemoryClient.ask` — memory.ask on the client every Agent
already has, reading across the namespace by default and crediting the cited
keys to the enclosing run scope."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from loom import Agent


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []
        self.ask_result: dict[str, Any] = {
            "answer": "Blue [m1].",
            "memory_keys": ["m1"],
            "source": "synthesized",
            "abstain": False,
            "results": [],
        }

    async def post(
        self, op_path: str, payload: dict[str, Any], idempotency_key=None, *, timeout_s=None
    ):
        self.calls.append((op_path, payload, timeout_s))
        if op_path == "/v1/run.begin":
            return {"run_id": "run-9"}
        if op_path == "/v1/run.complete":
            return {"run_id": payload["run_id"], "status": payload["status"]}
        if op_path == "/v1/memory.ask":
            return dict(self.ask_result)
        return {}

    async def close(self) -> None:
        pass

    def payload(self, op: str) -> dict[str, Any]:
        return [p for o, p, _ in self.calls if o == op][-1]


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x")
    a.transport = _FakeTransport()  # type: ignore[assignment]
    return a


async def test_ask_posts_memory_ask_across_the_namespace(agent: Agent) -> None:
    t: Any = agent.transport
    out = await agent.memory.ask("what colour is the sky?")
    p = t.payload("/v1/memory.ask")
    assert p["org"] == "o" and p["namespace"] == "n" and p["agent"] == "*"
    assert p["query"] == "what colour is the sky?" and p["top_k"] == 15
    assert p["allow_synthesis"] is True and p["run_id"] == "-"
    assert "read_scope" not in p and "bodies" not in p
    assert out["answer"] == "Blue [m1]." and out["memory_keys"] == ["m1"]


async def test_ask_forwards_the_optional_knobs(agent: Agent) -> None:
    t: Any = agent.transport
    when = datetime(2026, 9, 1, tzinfo=UTC)
    await agent.memory.ask(
        "q",
        top_k=3,
        agent_id="me",
        memory_type="semantic",
        allow_synthesis=False,
        max_answer_chars=300,
        bodies="unit",
        body_budget_tokens=800,
        search_mode="rrf",
        namespace_scope="all_mine",
        read_scope="own_user",
        user_id="u",
        session_id="s",
        run_id="r1",
        valid_as_of=when,
        known_as_of=when,
        timeout_s=90.0,
    )
    op, p, timeout = t.calls[-1]
    assert op == "/v1/memory.ask" and timeout == 90.0
    assert p["agent"] == "me" and p["top_k"] == 3 and p["memory_type"] == "semantic"
    assert p["allow_synthesis"] is False and p["max_answer_chars"] == 300
    assert p["bodies"] == "unit" and p["body_budget_tokens"] == 800
    assert p["search_mode"] == "rrf" and p["namespace_scope"] == "all_mine"
    assert p["read_scope"] == "own_user" and p["user_id"] == "u" and p["session_id"] == "s"
    assert p["run_id"] == "r1" and p["valid_as_of"] == when.isoformat()
    assert p["known_as_of"] == when.isoformat()


async def test_ask_inside_a_run_scope_credits_the_cited_keys(agent: Agent) -> None:
    t: Any = agent.transport
    async with agent.run_scope(goal="g") as run:
        await agent.memory.ask("q")
        assert run.used_keys == ["m1"]
    assert t.payload("/v1/memory.ask")["run_id"] == "run-9"
    assert t.payload("/v1/run.complete")["memories_used"] == ["m1"]


async def test_an_abstained_answer_credits_nothing(agent: Agent) -> None:
    t: Any = agent.transport
    t.ask_result = {"answer": "", "memory_keys": ["m1"], "source": "abstained", "abstain": True}
    async with agent.run_scope(goal="g") as run:
        await agent.memory.ask("q")
        assert run.used_keys == []
