"""A chunk hit obeys the read's filters.

`memory_chunks` carries no tags, `valid_at` or `status`; the fact leg's hard
tag filter, `as_of` anchor and served-status set reach the chunk leg through
the parent row in `memory_embeddings`, with the fact leg's own predicate
helpers and parameter names. Both stages carry the predicate. Without a
filter the leg is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tests.unit.test_memory_search_service import StubCH, _q

_PARENT_IN = "c.parent_memory_key IN (SELECT memory_key FROM loom.memory_embeddings FINAL"
_PARENT_NOT_IN = "c.parent_memory_key NOT IN (SELECT memory_key FROM loom.memory_embeddings FINAL"


def _cands(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [
        {"session_id": s, "chunk_index": i, "distance": 0.1 * (k + 1), "parent_memory_key": "p"}
        for k, (s, i) in enumerate(pairs)
    ]


async def _both_stages(q) -> tuple[tuple[str, dict], tuple[str, dict]]:
    from server.memory.app.retrieval.chunks import _search_memory_chunks

    ch = StubCH()
    ch.query_all_returns.append(_cands(("s1", 0), ("s2", 1)))
    ch.query_all_returns.append([])
    await _search_memory_chunks(ch, q, {"q": q.query_vec})
    assert len(ch.query_all_calls) == 2
    return ch.query_all_calls[0], ch.query_all_calls[1]


def test_no_filter_leaves_the_leg_unchanged() -> None:
    from server.memory.app.retrieval.chunks import _chunk_parent_pred

    params: dict[str, Any] = {}
    assert _chunk_parent_pred(_q(), params) == "" and params == {}


def test_soft_tags_are_a_boost_not_a_gate() -> None:
    from server.memory.app.retrieval.chunks import _chunk_parent_pred

    params: dict[str, Any] = {}
    assert _chunk_parent_pred(_q(tags=["workflow:w"], tags_soft=True), params) == ""


@pytest.mark.asyncio
async def test_hard_tags_gate_both_stages_through_the_parent() -> None:
    (sql1, p1), (sql2, p2) = await _both_stages(_q(tags=["workflow:w"], agent="bot"))
    for sql, params in ((sql1, p1), (sql2, p2)):
        assert _PARENT_IN in sql
        assert "hasAny(tags, {tags:Array(String)})" in sql  # the fact leg's tag_predicate
        assert "status IN (" in sql and "tombstoned_at IS NULL" in sql
        assert "agent = {agent:String}" in sql  # a private read stays per-agent
        assert params["tags"] == ["workflow:w"] and params["agent"] == "bot"
    # stage 1 stays the shape the vector index serves: the predicate is a
    # WHERE term, not a JOIN, and there is no FINAL on the chunk table itself
    assert "FROM loom.memory_chunks AS c\n" in sql1 and "JOIN" not in sql1


@pytest.mark.asyncio
async def test_as_of_with_tags_replays_the_parent() -> None:
    at = datetime(2026, 8, 25, 12, 0, 0)
    (sql1, p1), (_sql2, _p2) = await _both_stages(_q(tags=["workflow:w"], as_of=at))
    assert _PARENT_IN in sql1 and "{as_of:DateTime}" in sql1
    assert p1["as_of"] == at and p1["tags"] == ["workflow:w"]


@pytest.mark.asyncio
async def test_as_of_alone_excludes_parents_not_valid_then_and_keeps_orphans() -> None:
    at = datetime(2026, 8, 25, 12, 0, 0)
    (sql1, p1), (sql2, _p2) = await _both_stages(_q(as_of=at))
    for sql in (sql1, sql2):
        assert _PARENT_NOT_IN in sql and "NOT (" in sql and "{as_of:DateTime}" in sql
        assert "hasAny(tags" not in sql
    assert p1["as_of"] == at


def test_cross_agent_read_has_no_agent_term() -> None:
    from server.memory.app.retrieval.chunks import _chunk_parent_pred

    params: dict[str, Any] = {}
    pred = _chunk_parent_pred(_q(tags=["t"], agent="*"), params)
    assert "agent =" not in pred and "agent" not in params


def test_duplicates_admitted_only_when_asked() -> None:
    from server.memory.app.retrieval.chunks import _chunk_parent_pred

    strict = _chunk_parent_pred(_q(tags=["t"]), {})
    loose = _chunk_parent_pred(_q(tags=["t"], include_duplicates=True), {})
    assert "'duplicate'" not in strict and "'duplicate'" in loose


@pytest.mark.asyncio
async def test_a_session_bound_read_gates_both_stages_on_the_session() -> None:
    # An own_session read scope pins session_id; the chunk table carries the
    # column, so another session's transcript never reaches the leg.
    (sql1, p1), (sql2, p2) = await _both_stages(_q(session_id="s1"))
    for sql, params in ((sql1, p1), (sql2, p2)):
        assert "AND c.session_id = {session_id:String}" in sql
        assert params["session_id"] == "s1"
    (sql1, p1), (_sql2, _p2) = await _both_stages(_q(session_ids=["s1", "s2"]))
    assert "AND c.session_id IN {session_ids:Array(String)}" in sql1
    assert p1["session_ids"] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_no_session_bound_leaves_the_leg_unsessioned() -> None:
    (sql1, p1), (sql2, _p2) = await _both_stages(_q())
    assert "c.session_id = {session_id" not in sql1 + sql2
    assert "session_id" not in p1


@pytest.mark.parametrize(
    "bounds",
    [
        {},
        {"user_id": "u1"},
        {"session_id": "s1", "session_ids": ["s1", "s2"]},
        {"run_id": "r1", "tags": ["t"]},
        {"as_of": datetime(2020, 1, 1)},
    ],
)
def test_the_neighbour_fallback_binds_every_parameter_its_predicate_names(
    bounds: dict[str, Any],
) -> None:
    """The third statement (an anchor stage 2 did not prefetch) carries the leg's identity
    and parent predicates; every placeholder they name must be bound, or the statement
    fails and the anchors lose their neighbour windows."""
    import re

    from server.memory.app.retrieval.chunks import (
        _chunk_identity_pred,
        _chunk_neighbor_sql,
        _chunk_parent_pred,
    )

    q = _q(query_text="alpha beta", **bounds)
    params: dict[str, Any] = {"q": q.query_vec, "org": q.org, "ns": q.namespace, "model": "m"}
    pred = _chunk_identity_pred(q, params)
    if parent := _chunk_parent_pred(q, params):
        pred = f"{pred}\n{parent}"
    params["chunk_limit"] = 30  # stage-only keys stay out of the neighbour statement
    sql, bound = _chunk_neighbor_sql([("s1", 3)], chunk_agent_pred=pred, base_params=params)
    named = set(re.findall(r"\{(\w+):", sql))
    assert named <= set(bound), named - set(bound)
    assert "chunk_limit" not in bound
