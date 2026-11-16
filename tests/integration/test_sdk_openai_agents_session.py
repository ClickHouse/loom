"""LoomSession against a running Loom: items added, read, popped and cleared are
episodic memories of the user's Loom session, read back as written."""

from __future__ import annotations

import asyncio

import pytest

# An SDK extra, outside the server's environment: install openai-agents to run these.
pytest.importorskip("agents")

from loom.integrations.openai_agents import LoomSession


async def test_a_session_keeps_its_items_in_loom(agent) -> None:
    session = LoomSession(agent, "trip-42", user_id="alice")
    items = [
        {"role": "user", "content": "I prefer aisle seats"},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "I prefer aisle seats"},
    ]
    await session.add_items(items)
    assert await session.get_items() == items
    rows = await agent.memory.list(
        prefix="openai-agents/",
        memory_type="episodic",
        user_id="alice",
        session_id=session._loom_session,
        fields=["memory_key", "value", "memory_type", "user_id", "session_id"],
    )
    assert [(r["memory_type"], r["user_id"], r["session_id"]) for r in rows["entries"]] == [
        ("episodic", "alice", session._loom_session)
    ] * 3
    other = LoomSession(agent, "trip-42", user_id="bob")
    assert await other.get_items() == []

    # Items added a second later, one past the list's 2,048-character preview, still come
    # back in the order they were added and whole.
    await asyncio.sleep(1.1)
    later = [{"role": "assistant", "content": "x" * 5000}, {"role": "user", "content": "last"}]
    await session.add_items(later)
    assert await session.get_items() == [*items, *later]
    assert await session.get_items(limit=2) == later
    assert await session.pop_item() == later[1]
    assert await session.pop_item() == later[0]

    assert await session.pop_item() == items[2]
    assert await session.get_items(limit=1) == [items[1]]
    await session.clear_session()
    assert await session.get_items() == []
