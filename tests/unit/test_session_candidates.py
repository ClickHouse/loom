"""A search restricted to a set of sessions filters on them."""

from __future__ import annotations

from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery


def test_session_set_predicate_in_where():
    q = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.1],
        top_k=5,
        session_ids=["s1", "s2"],
    )
    where, params = _build_where(q)
    assert "e.session_id IN {session_ids:Array(String)}" in where
    assert params["session_ids"] == ["s1", "s2"]
    q2 = SearchQuery(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.1],
        top_k=5,
    )
    where2, params2 = _build_where(q2)
    assert not any("session_ids" in w for w in where2) and "session_ids" not in params2
