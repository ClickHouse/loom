"""Server-free coverage that MemoryClient.set forwards `entities`.

The wire op (MemorySetInput.entities) stores client-supplied entities on the
embedding row so `entity_filter` / `memory.timeline` find them without waiting
on LLM extraction. The SDK must pass them through — and omit the key when empty
so the server default applies, mirroring the `tags` guard. (ClickGap relies on
this for identifier-anchored recall — error codes, system tables, symbols.)
"""

from __future__ import annotations

import pytest
from loom import Agent


class _CaptureTransport:
    def __init__(self) -> None:
        self.payload: dict | None = None

    async def post(self, op_path, payload, idempotency_key=None, *, timeout_s=None):
        self.payload = payload
        return {"version": 1, "memory_id": "m1", "content_sha256": "s"}

    async def close(self):  # pragma: no cover - parity with Transport
        pass


@pytest.fixture
def agent():
    return Agent(org="o", namespace="n", agent="ag", api_key="x")


async def test_set_forwards_entities(agent):
    agent.transport = _CaptureTransport()
    await agent.memory.set(
        "lesson_1",
        {"text": "Nullable read flagged in MergeTree"},
        tags=["kind:false_positive"],
        entities=["MergeTree", "Nullable"],
    )
    assert agent.transport.payload["entities"] == ["MergeTree", "Nullable"]
    # entities and tags are independent axes — both ride the same write.
    assert agent.transport.payload["tags"] == ["kind:false_positive"]


async def test_set_omits_entities_when_empty(agent):
    agent.transport = _CaptureTransport()
    await agent.memory.set("k", {"text": "x"})
    assert "entities" not in agent.transport.payload
