"""loom_toolset against a running Loom: the toolset's loom_remember stores a
fact, and its loom_recall reads it back."""

from __future__ import annotations

import json

import pytest

from tests.integration._eventually import eventually

# An SDK extra, outside the server's environment: install pydantic-ai-slim to run these.
pytest.importorskip("pydantic_ai")

from loom.integrations.pydantic_ai import loom_toolset


async def test_remember_then_recall(agent) -> None:
    tools = dict(loom_toolset(agent, user_id="alice").tools)
    stored = json.loads(
        await tools["loom_remember"].function(
            memory_key="seat_preference", value="Alice prefers aisle seats on long flights"
        )
    )
    assert stored["stored"] is True

    async def recalled() -> bool:
        out = json.loads(await tools["loom_recall"].function(query="Alice aisle seat"))
        return [m["memory_key"] for m in out["memories"]][:1] == ["seat_preference"]

    # The write is searchable once its embedding lands.
    await eventually(recalled, description="seat_preference recalled")
