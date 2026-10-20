"""Unit tests for server/knowledge/app/conversation_search.py."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.knowledge.app.conversation_search import (
    ConversationHit,
    ConversationVectorQuery,
    vector_search,
)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []


def _q(**overrides) -> ConversationVectorQuery:
    base = {
        "org": "clickhouse",
        "namespace": "ns",
        "agent": "bot",
        "embedding_model": "text-embedding-3-small",
        "query_vec": [0.1] * 1536,
        "top_k": 10,
    }
    base.update(overrides)
    return ConversationVectorQuery(**base)


def _row() -> dict[str, Any]:
    return {
        "thread_id": "t-1",
        "msg_id": "m-7",
        "role": "user",
        "excerpt": "deploy soon?",
        "ts": datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
        "distance": 0.18,
    }


@pytest.mark.asyncio
async def test_vector_search_default_filters_and_order() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_row()])
    out = await vector_search(_q(), ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "FROM loom.conversation_embeddings FINAL" in sql
    assert "embedding_dim > 0" in sql
    # Single bare-distance sort key so CH's vector-search rewrite stays
    # eligible; the recency tie-break moved to Python.
    assert "ORDER BY distance ASC" in sql
    assert "ts DESC" not in sql
    assert "hnsw_candidate_list_size_for_search" in sql
    # Relevance gate: rows beyond max_distance are dropped in SQL.
    assert "cosineDistance(embedding, {q:Array(Float32)}) <= {maxd:Float64}" in sql
    assert params["model"] == "text-embedding-3-small"
    assert len(out) == 1 and isinstance(out[0], ConversationHit)
    # The row stores the embedded text; a hit shows its first 240 characters.
    assert "substringUTF8(content_excerpt, 1, {excerpt:UInt32}) AS excerpt" in sql
    assert params["excerpt"] == 240
    assert out[0].content_excerpt == "deploy soon?"


@pytest.mark.asyncio
async def test_optional_filters_thread_through() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(
        _q(
            thread_id="t-9",
            role="assistant",
            run_id="r-1",
            user_id="u-1",
        ),
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "thread_id = {thread_id:String}" in sql
    assert "role = {role:String}" in sql
    assert "run_id = {run_id:String}" in sql
    assert "user_id = {user_id:String}" in sql
    assert params["thread_id"] == "t-9"
    assert params["role"] == "assistant"
    assert params["run_id"] == "r-1"
    assert params["user_id"] == "u-1"


@pytest.mark.asyncio
async def test_no_optional_filters_omits_clauses() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "thread_id = " not in sql
    assert "role = " not in sql
    assert "run_id = " not in sql
    assert "user_id = " not in sql


@pytest.mark.asyncio
async def test_explicit_agent_filters_but_wildcard_spans_all() -> None:
    """An explicit agent scopes to its own threads; '*'/'-' drop the predicate
    so an operator browse spans every agent (mirrors memory.search)."""
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(agent="bot"), ch=ch)
    sql, params = ch.query_all_calls[-1]
    assert "agent = {agent:String}" in sql
    assert params["agent"] == "bot"

    for star in ("*", "-"):
        ch.query_all_returns.append([])
        await vector_search(_q(agent=star), ch=ch)
        sql, params = ch.query_all_calls[-1]
        assert "agent = {agent:String}" not in sql
        assert "agent" not in params


@pytest.mark.asyncio
async def test_recency_breaks_distance_ties_in_python() -> None:
    """Equal-distance rows come back newest-first.

    The tie-break runs in Python: a second SQL sort key (`, ts DESC`) would
    disqualify the query from ClickHouse's vector-search rewrite, which bails
    on any sort with more than one key. This pins the ordering so nobody moves
    it back into the SQL.
    """
    older = {**_row(), "msg_id": "m-old", "ts": datetime(2026, 1, 1, tzinfo=UTC)}
    newer = {**_row(), "msg_id": "m-new", "ts": datetime(2026, 6, 1, tzinfo=UTC)}
    nearer = {
        **_row(),
        "msg_id": "m-near",
        "distance": 0.01,
        "ts": datetime(2020, 1, 1, tzinfo=UTC),
    }
    ch = StubCH()
    # CH hands back distance-ordered rows; the tie pair arrives oldest-first.
    ch.query_all_returns.append([nearer, older, newer])
    out = await vector_search(_q(), ch=ch)
    assert [h.msg_id for h in out] == ["m-near", "m-new", "m-old"]


@pytest.mark.asyncio
async def test_tolerates_non_datetime_ts_in_tie_break() -> None:
    """A string or missing ts must not raise inside the sort comparator."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {**_row(), "msg_id": "a", "ts": "2026-06-01T00:00:00+00:00"},
            {**_row(), "msg_id": "b", "ts": None},
        ]
    )
    out = await vector_search(_q(), ch=ch)
    assert {h.msg_id for h in out} == {"a", "b"}


@pytest.mark.asyncio
async def test_the_substring_fallback_cuts_a_hit_as_the_vector_path_does(monkeypatch) -> None:
    """With the embedder down, a hit is the turn's first 240 characters, no ellipsis."""
    from server.knowledge.api.models_conversations import ConversationSearchInput
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo

    content = "deploy " * 100  # 700 characters

    async def fake_search_substring(*a, **k):
        return [
            conversation_repo.MessageRow(
                org="o",
                namespace="ns",
                agent="a",
                thread_id="t",
                msg_id="m1",
                role="user",
                content=content,
                ts=datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
            )
        ]

    monkeypatch.setattr(conversation_repo, "search_substring", fake_search_substring)
    out = await h._search_substring_fallback(
        ConversationSearchInput(org="o", namespace="ns", agent="a", query="deploy"),
        ["ns"],
        [],
        saved_only=False,
    )
    [hit] = out.hits
    assert hit.content_excerpt == content[:240]


@pytest.mark.asyncio
async def test_a_live_excerpt_length_reaches_both_cuts(monkeypatch) -> None:
    """conversation_search_excerpt_chars is read per search, by the vector path and the
    fallback alike."""
    from server.knowledge.api.models_conversations import ConversationSearchInput
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo
    from server.settings import settings

    monkeypatch.setattr(settings, "conversation_search_excerpt_chars", 12)
    ch = StubCH()
    ch.query_all_returns.append([_row()])
    await vector_search(_q(), ch=ch)
    assert ch.query_all_calls[0][1]["excerpt"] == 12

    content = "deploy " * 100

    async def fake_search_substring(*a, **k):
        return [
            conversation_repo.MessageRow(
                org="o",
                namespace="ns",
                agent="a",
                thread_id="t",
                msg_id="m1",
                role="user",
                content=content,
                ts=datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
            )
        ]

    monkeypatch.setattr(conversation_repo, "search_substring", fake_search_substring)
    out = await h._search_substring_fallback(
        ConversationSearchInput(org="o", namespace="ns", agent="a", query="deploy"), ["ns"], []
    )
    assert out.hits[0].content_excerpt == content[:12]


def test_the_excerpt_setting_is_bounded_by_what_a_row_stores() -> None:
    """server.settings cannot import the knowledge plane, so the bound is a copy."""
    from annotated_types import Le

    from server.knowledge.domain.conversations import EMBEDDED_EXCERPT_CHARS
    from server.settings import Settings

    field = Settings.model_fields["conversation_search_excerpt_chars"]
    assert [m.le for m in field.metadata if isinstance(m, Le)] == [EMBEDDED_EXCERPT_CHARS]
