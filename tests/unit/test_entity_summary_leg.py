"""memory.search's entity-summary leg: the summary plane's vocabulary and
its identity scoping."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_summary_vocabulary_is_the_summary_plane_alone() -> None:
    from server.memory.app.retrieval import summaries as MS

    class _SCH:
        async def query_all(self, sql: str, params=None):
            assert "entity_communities" not in sql
            return [{"e": "community:caroline"}]

    MS._SUMMARY_ENTITY_CACHE.clear()
    assert await MS.summary_entities_present(_SCH(), "o", ["n"]) == frozenset(
        {"community:caroline"}
    )
    MS._SUMMARY_ENTITY_CACHE.clear()


@pytest.mark.asyncio
async def test_entity_summary_leg_admits_shared_rows_for_a_named_agent() -> None:
    from server.memory.app import retrieval as MS

    class _RCH:
        def __init__(self) -> None:
            self.sql, self.params = "", {}

        async def query_all(self, sql: str, params=None):
            self.sql, self.params = sql, params or {}
            return []

    ch = _RCH()
    await MS.fetch_entity_summaries(
        ch,
        org="o",
        namespace="n",
        agent="locomo-bench",
        user_id=None,
        query_vec=[0.1],
        embedding_model="m",
        entity_filter=["caroline"],
        n=5,
    )
    assert "agent IN ({agent:String}, '*')" in ch.sql and ch.params["agent"] == "locomo-bench"
    assert ch.params["uid"] == "-"
