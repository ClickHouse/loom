"""Server-free unit coverage for MemoryClient.outcome_by_key.

outcome_by_key bridges the recall→reinforce gap: recall returns memory_key, and
the server resolves key→id (namespace-wide, agent="*"), so this is a SINGLE
memory.outcome call carrying memory_key — no client-side get round-trip. The
fake transport asserts the shape and that the server response passes through.
"""

from __future__ import annotations

import pytest
from loom import Agent


class _FakeTransport:
    def __init__(self, outcome_response=None):
        self.calls = []
        self._outcome_response = outcome_response or {
            "recorded": True,
            "evidence_count_after": 3,
        }

    async def post(self, op_path, payload, idempotency_key=None, *, timeout_s=None):
        self.calls.append((op_path, payload))
        if op_path == "/v1/memory.outcome":
            return self._outcome_response
        return {}

    async def close(self):  # pragma: no cover - parity with Transport
        pass


@pytest.fixture
def agent():
    return Agent(org="o", namespace="n", agent="ag", api_key="x")


async def test_outcome_by_key_single_call_with_key(agent):
    agent.transport = _FakeTransport()
    res = await agent.memory.outcome_by_key("cg_lesson_a", outcome="positive", source="t")
    assert res["recorded"] is True
    # Single call — the server resolves key→id, no client-side get.
    assert [c[0] for c in agent.transport.calls] == ["/v1/memory.outcome"]
    payload = agent.transport.calls[-1][1]
    assert payload["memory_key"] == "cg_lesson_a"
    assert "memory_id" not in payload
    assert payload["outcome"] == "positive"
    assert payload["source"] == "t"


async def test_outcome_by_key_passes_server_noop_through(agent):
    # Server returns recorded=False when the key doesn't resolve — pass it back.
    agent.transport = _FakeTransport({"recorded": False, "evidence_count_after": 0})
    res = await agent.memory.outcome_by_key("missing")
    assert res["recorded"] is False
    assert [c[0] for c in agent.transport.calls] == ["/v1/memory.outcome"]


async def test_outcome_by_key_negative_passes_direction(agent):
    agent.transport = _FakeTransport()
    await agent.memory.outcome_by_key("k", outcome="negative")
    assert agent.transport.calls[-1][1]["outcome"] == "negative"


async def test_outcome_by_key_takes_no_session(agent):
    # memory.outcome has no session_id field: a kwarg for it would be silently dropped.
    agent.transport = _FakeTransport()
    with pytest.raises(TypeError):
        await agent.memory.outcome_by_key("k", session_id="s")  # type: ignore[call-arg]
