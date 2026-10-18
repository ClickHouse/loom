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
        ),
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "thread_id = {thread_id:String}" in sql
    assert "role = {role:String}" in sql
    assert "run_id = {run_id:String}" in sql
    assert params["thread_id"] == "t-9"
    assert params["role"] == "assistant"
    assert params["run_id"] == "r-1"


@pytest.mark.asyncio
async def test_no_optional_filters_omits_clauses() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await vector_search(_q(), ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "thread_id = " not in sql
    assert "role = " not in sql
    assert "run_id = " not in sql


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
async def test_the_embedding_row_keeps_the_text_its_vector_was_built_from(monkeypatch) -> None:
    """A re-embed rebuilds a conversation vector from `content_excerpt`, so the row
    holds the text append embedded, not the shorter excerpt a hit shows."""
    from server.knowledge.api.models_conversations import ConversationAppendInput
    from server.knowledge.app import conversations as h
    from server.knowledge.store import conversations as conversation_repo

    async def enabled(org, ns, **k):
        return True

    async def absent(*a, **k):
        return False

    async def fake_append(*a, **k):
        return None

    rows: list[dict] = []

    async def fake_insert_embedding(row, **k):
        rows.append(row)

    class _Embedder:
        model_name = "fake"
        dim = 3

        def __init__(self) -> None:
            self.embedded: list[str] = []

        async def embed(self, text):
            self.embedded.append(text)
            return [0.1, 0.2, 0.3]

    embedder = _Embedder()
    monkeypatch.setattr(conversation_repo, "namespace_logging_enabled", enabled)
    monkeypatch.setattr(conversation_repo, "message_exists", absent)
    monkeypatch.setattr(conversation_repo, "append", fake_append)
    monkeypatch.setattr(conversation_repo, "insert_embedding", fake_insert_embedding)
    monkeypatch.setattr(h, "get_embedder", lambda: embedder)

    content = "a" * 300 + " the deploy moved to friday " + "b" * 3000
    await h.handle_conversation_append(
        ConversationAppendInput(
            org="dev",
            namespace="x",
            agent="a",
            thread_id="t",
            msg_id="m3",
            role="user",
            content=content,
        )
    )
    [row] = rows
    assert embedder.embedded == [content[:2048]]
    assert row["content_excerpt"] == content[:2048]
