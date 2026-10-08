"""The graph walk can start from what was said last."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.app import recent_seeds as R


def test_query_is_scoped_to_the_identity_and_bounded() -> None:
    sql, params = R.build_query(
        org="acme",
        namespace="shop",
        agent="bot",
        user_id="u1",
        session_id="s9",
        memories=5,
        limit=8,
    )
    assert "loom.memory FINAL" in sql and "notEmpty(entities)" in sql
    assert "agent = {agent:String}" in sql and "user_id = {uid:String}" in sql
    assert "session_id = {sid:String}" in sql
    assert params == {
        "org": "acme",
        "ns": "shop",
        "n": 5,
        "lim": 8,
        "agent": "bot",
        "uid": "u1",
        "sid": "s9",
    }
    sql, params = R.build_query(
        org="acme",
        namespace="shop",
        agent="*",
        user_id="-",
        session_id=None,
        memories=0,
        limit=0,
    )
    assert "agent =" not in sql and "user_id =" not in sql and "session_id =" not in sql
    assert params["n"] == 1 and params["lim"] == 1


@pytest.mark.asyncio
async def test_entities_come_back_lowercased_deduped_and_fail_open() -> None:
    class _CH:
        async def query_all(self, sql, params=None):
            return [
                {"entity": "Caroline"},
                {"entity": "paris"},
                {"entity": "caroline"},
                {"entity": ""},
            ]

    assert await R.recent_seed_entities(org="o", namespace="n", ch=_CH()) == ["caroline", "paris"]

    class _Boom:
        async def query_all(self, sql, params=None):
            raise RuntimeError("ch down")

    assert await R.recent_seed_entities(org="o", namespace="n", ch=_Boom()) == []


def test_mode_resolution_and_input_field() -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.graph import _graph_seed_mode
    from server.settings import Settings

    assert Settings().memory_search_graph_seed == "query"
    assert _graph_seed_mode(SimpleNamespace(graph_seed="")) == "query"
    assert _graph_seed_mode(SimpleNamespace(graph_seed="recent")) == "recent"
    assert _graph_seed_mode(SimpleNamespace(graph_seed="weird")) == "query"
    base = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "query": "q",
    }
    assert MemorySearchInput(**base, graph_seed="both").graph_seed == "both"
