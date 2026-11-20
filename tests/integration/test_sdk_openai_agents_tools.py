"""loom_tools against a running Loom: a fact the model stores with
loom_remember comes back through loom_recall."""

from __future__ import annotations

import asyncio
import json

import pytest

# An SDK extra, outside the server's environment: install openai-agents to run these.
pytest.importorskip("agents")

from agents.tool_context import ToolContext
from loom.integrations.openai_agents import loom_tools


async def _invoke(tool, arguments: dict) -> str:
    raw = json.dumps(arguments)
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="c", tool_arguments=raw)
    return await tool.on_invoke_tool(ctx, raw)


async def test_remember_then_recall(agent) -> None:
    tools = {t.name: t for t in loom_tools(agent, user_id="alice")}
    stored = json.loads(
        await _invoke(
            tools["loom_remember"],
            {"memory_key": "seat_preference", "value": "Alice prefers aisle seats on long flights"},
        )
    )
    assert stored["stored"] is True
    keys: list[str] = []
    for _ in range(20):  # the write is searchable once its embedding lands
        out = json.loads(await _invoke(tools["loom_recall"], {"query": "Alice aisle seat"}))
        keys = [m["memory_key"] for m in out["memories"]]
        if keys:
            break
        await asyncio.sleep(0.25)
    assert keys[:1] == ["seat_preference"]
