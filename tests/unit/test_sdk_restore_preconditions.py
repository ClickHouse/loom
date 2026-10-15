"""MemoryClient.restore forwards the Undo preconditions, and only when set."""

from __future__ import annotations

from loom import Agent


class _CaptureTransport:
    def __init__(self) -> None:
        self.payload: dict | None = None

    async def post(self, op_path, payload, idempotency_key=None, *, timeout_s=None):
        self.payload = payload
        return {"restored": True, "from_status": "superseded"}

    async def close(self):  # pragma: no cover - parity with Transport
        pass


async def test_restore_sends_its_preconditions() -> None:
    agent = Agent(org="o", namespace="n", agent="ag", api_key="x")
    agent.transport = _CaptureTransport()
    await agent.memory.restore("k", if_superseded_by="m2", only_if_superseded=True)
    assert agent.transport.payload["if_superseded_by"] == "m2"
    assert agent.transport.payload["only_if_superseded"] is True


async def test_restore_leaves_unset_preconditions_to_the_server() -> None:
    agent = Agent(org="o", namespace="n", agent="ag", api_key="x")
    agent.transport = _CaptureTransport()
    await agent.memory.restore("k")
    assert "if_superseded_by" not in agent.transport.payload
    assert "only_if_superseded" not in agent.transport.payload
