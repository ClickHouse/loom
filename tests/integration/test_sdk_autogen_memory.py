"""LoomMemory against a running Loom: a memory added through AutoGen's
protocol comes back through query."""

from __future__ import annotations

import pytest

from tests.integration._eventually import eventually

# An SDK extra, outside the server's environment: install autogen-core to run these.
pytest.importorskip("autogen_core")

from autogen_core.memory import MemoryContent, MemoryMimeType
from loom.integrations.autogen import LoomMemory


async def _recalled(memory: LoomMemory, text: str, word: str) -> bool:
    result = await memory.query(text)
    return any(word in str(r.content) for r in result.results)


async def test_add_then_query(agent) -> None:
    memory = LoomMemory(agent, user_id="alice", honour_abstain=False)
    await memory.add(
        MemoryContent(
            content="Alice prefers aisle seats on long flights", mime_type=MemoryMimeType.TEXT
        )
    )
    # A write is searchable once its embedding lands; eventually raises on timeout.
    await eventually(
        lambda: _recalled(memory, "Alice aisle seat", "aisle"), description="'aisle' recalled"
    )
