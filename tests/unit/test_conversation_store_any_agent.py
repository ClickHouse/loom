"""The conversation store across agents: the substring fallback spans every agent on
"*"/"-", as the vector search does, and a thread read with no agent reads every
agent's turns."""

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
async def test_thread_reads_drop_the_agent_filter_for_any_agent() -> None:
    reads = (
        lambda agent, ch: conversation_repo.fetch_thread("o", "ns", agent, "t-1", ch=ch),
        lambda agent, ch: conversation_repo.count_thread_msgs("o", "ns", agent, "t-1", ch=ch),
        lambda agent, ch: conversation_repo.list_thread("o", "ns", agent, "t-1", ch=ch),
    )
    for read in reads:
        ch = StubCH()
        await read("bot", ch)
        sql, params = ch.calls[-1]
        assert "agent = {agent:String}" in sql
        assert params["agent"] == "bot"

        await read(None, ch)
        sql, params = ch.calls[-1]
        assert "agent = {agent:String}" not in sql
        assert "agent" not in params
        assert "thread_id = {tid:String}" in sql
