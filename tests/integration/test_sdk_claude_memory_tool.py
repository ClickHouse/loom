"""The Claude memory tool over a running Loom: files created, edited, renamed and
deleted through the tool are Loom documents, read back as written, found by
knowledge.search, and kept apart per store."""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration._eventually import eventually

# An SDK extra, outside the server's environment: install anthropic to run these.
pytest.importorskip("anthropic")

from anthropic.lib.tools import ToolError
from loom.integrations.claude_memory import AsyncLoomMemoryTool


async def test_a_file_lives_as_a_document_through_every_command(agent) -> None:
    tool = AsyncLoomMemoryTool(agent, store="alice")

    async def run(command: dict[str, Any]) -> Any:
        return await tool.call(command)

    await run(
        {
            "command": "create",
            "path": "/memories/trip.md",
            "file_text": "Seat: aisle\nCity: Lisbon\n",
        }
    )
    doc = await agent.knowledge.get("loom-memory://alice/memories/trip.md")
    assert doc is not None
    assert (doc["content"], doc["tags"]) == ("Seat: aisle\nCity: Lisbon\n", ["claude-memory:alice"])

    await run(
        {
            "command": "str_replace",
            "path": "/memories/trip.md",
            "old_str": "Lisbon",
            "new_str": "Porto",
        }
    )
    await run(
        {
            "command": "insert",
            "path": "/memories/trip.md",
            "insert_line": 0,
            "insert_text": "# Trip",
        }
    )
    assert (await run({"command": "view", "path": "/memories/trip.md"})).endswith(
        "     1\t# Trip\n     2\tSeat: aisle\n     3\tCity: Porto\n     4\t"
    )

    async def found() -> bool:
        hits = await agent.knowledge.search("Porto seat aisle", top_k=5)
        return any(h.source_uri == "loom-memory://alice/memories/trip.md" for h in hits)

    await eventually(found, description="the file found by knowledge.search")

    await run(
        {"command": "rename", "old_path": "/memories/trip.md", "new_path": "/memories/trips/may.md"}
    )
    assert await agent.knowledge.get("loom-memory://alice/memories/trip.md") is None
    assert (await run({"command": "view", "path": "/memories"})).endswith(
        "/memories\n/memories/trips/\n/memories/trips/may.md"
    )
    other = AsyncLoomMemoryTool(agent, store="bob")
    with pytest.raises(ToolError, match="does not exist"):
        await other.call({"command": "view", "path": "/memories/trips/may.md"})

    await run({"command": "delete", "path": "/memories/trips"})
    assert await agent.knowledge.get("loom-memory://alice/memories/trips/may.md") is None
    assert (await run({"command": "view", "path": "/memories"})).endswith("/memories")
