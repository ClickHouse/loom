"""The conversation store's "*" agent: the substring fallback spans every agent, as
the vector search does, and `thread_agent` names the agent a thread was written by."""

from __future__ import annotations

from typing import Any

import pytest

from server.knowledge.store import conversations as conversation_repo


class StubCH:
    def __init__(self, rows: list[list[dict[str, Any]]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return self.rows.pop(0) if self.rows else []

    async def query_one(self, sql, params=None):
        found = await self.query_all(sql, params)
        return found[0] if found else None


@pytest.mark.asyncio
async def test_substring_search_spans_every_agent_on_a_wildcard() -> None:
    ch = StubCH()
    await conversation_repo.search_substring("o", ["ns"], "bot", "deploy", ch=ch)
    sql, params = ch.calls[-1]
    assert "agent = {agent:String}" in sql
    assert params["agent"] == "bot"

    for star in ("*", "-"):
        await conversation_repo.search_substring("o", ["ns"], star, "deploy", ch=ch)
        sql, params = ch.calls[-1]
        assert "agent = {agent:String}" not in sql
        assert "agent" not in params


@pytest.mark.asyncio
async def test_thread_agent_reads_messages_then_the_thread_row() -> None:
    ch = StubCH([[{"agent": "reviewer"}]])
    assert await conversation_repo.thread_agent("o", "ns", "t-1", ch=ch) == "reviewer"
    assert len(ch.calls) == 1

    # A thread created with no messages yet is named by its metadata row.
    ch = StubCH([[{"agent": ""}], [{"agent": "planner"}]])
    assert await conversation_repo.thread_agent("o", "ns", "t-2", ch=ch) == "planner"

    ch = StubCH([[{"agent": ""}], []])
    assert await conversation_repo.thread_agent("o", "ns", "ghost", ch=ch) is None
