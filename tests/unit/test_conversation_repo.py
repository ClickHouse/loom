"""Unit tests for server/knowledge/store/conversations.py — mocked CH client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.ch_writes import batched_insert_settings
from server.core.vectors import codes_state
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.knowledge.store import conversations
from server.knowledge.store.conversations import (
    MessageInsert,
    MessageRow,
    append,
    embedding_insert_settings,
    list_thread,
    search_substring,
)
from server.settings import settings


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


def _msg_row() -> dict[str, Any]:
    return {
        "org": "clickhouse",
        "namespace": "ns",
        "agent": "claude-desktop",
        "thread_id": "t-1",
        "msg_id": "m-1",
        "role": "user",
        "content": "hi",
        "user_id": "-",
        "metadata": "{}",
        "tokens": 4,
        "ts": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_append_omits_ts_when_none() -> None:
    ch = StubCH()
    msg = MessageInsert(
        org="clickhouse",
        namespace="ns",
        agent="bot",
        thread_id="t-1",
        msg_id="m-1",
        role="user",
        content="hi",
    )
    await append(msg, ch=ch)
    _, rows, settings = ch.insert_calls[0]
    assert "ts" not in rows[0]  # CH defaults to now()
    assert settings == batched_insert_settings()


@pytest.mark.asyncio
async def test_an_append_once_per_identity_is_committed_with_the_identity_token() -> None:
    """Every writer of one turn's identity sends it committed: a replicated table
    deduplicates committed and buffered inserts separately."""
    ch = StubCH()
    msg = MessageInsert(
        org="clickhouse",
        namespace="ns",
        agent="bot",
        thread_id="t-1",
        msg_id="m-1",
        role="user",
        content="hi",
    )
    await append(msg, once_per_identity=True, ch=ch)
    await append(msg, once_per_identity=True, ch=ch)
    (_, _, first), (_, _, second) = ch.insert_calls
    assert first["async_insert"] == 0 and first["wait_for_async_insert"] == 1
    assert first["insert_deduplication_token"].startswith("conversation-turn:")
    assert first == second


COMMITTED = {"async_insert": 0, "wait_for_async_insert": 1}
EMBEDDING = "loom.conversation_embeddings.embedding"


def test_an_embedding_row_is_batched_only_when_its_width_cannot_fail_the_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "embedding_dim", 4)
    # Never probed: ClickHouse judges the width, so the row goes alone.
    assert embedding_insert_settings([0.1] * 4) == COMMITTED
    # A plain column takes any width.
    codes_state.set_coded(set())
    assert embedding_insert_settings([0.1] * 3) == batched_insert_settings()
    # Coded at the served model's width: a full vector and an empty one (padded).
    codes_state.set_coded({EMBEDDING})
    monkeypatch.setattr(codes_state._state, "widths", {EMBEDDING: 4})
    assert embedding_insert_settings([0.1] * 4) == batched_insert_settings()
    assert embedding_insert_settings([]) == batched_insert_settings()
    # A vector of another width, or a model change pending a rebuild.
    assert embedding_insert_settings([0.1] * 3) == COMMITTED
    monkeypatch.setattr(settings, "embedding_dim", 8)
    assert embedding_insert_settings([0.1] * 4) == COMMITTED


@pytest.mark.asyncio
async def test_an_embedding_row_in_a_flush_another_width_failed_is_sent_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        conversations, "embedding_insert_settings", lambda _v: batched_insert_settings()
    )

    class FlushRefused(StubCH):
        async def insert(self, table, rows, settings=None):
            await super().insert(table, rows, settings)
            if settings != COMMITTED:
                raise RuntimeError("Code: 190. Elements of the vector have a different width")

    ch = FlushRefused()
    row: Any = {"org": "o", "msg_id": "m-1", "embedding": [0.1] * 4}
    await conversations.insert_embedding(row, ch=ch)
    assert [s for _, _, s in ch.insert_calls] == [batched_insert_settings(), COMMITTED]


@pytest.mark.asyncio
async def test_append_includes_ts_when_provided() -> None:
    ch = StubCH()
    ts = datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC)
    msg = MessageInsert(
        org="clickhouse",
        namespace="ns",
        agent="bot",
        thread_id="t-1",
        msg_id="m-1",
        role="assistant",
        content="hello",
        ts=ts,
    )
    await append(msg, ch=ch)
    _, rows, _ = ch.insert_calls[0]
    assert rows[0]["ts"] == ts


@pytest.mark.asyncio
async def test_list_thread_orders_ascending_for_replay() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_msg_row(), _msg_row()])
    rows = await list_thread("clickhouse", "ns", "claude-desktop", "t-1", ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "ORDER BY ts ASC, msg_id ASC" in sql
    assert len(rows) == 2 and all(isinstance(r, MessageRow) for r in rows)


@pytest.mark.asyncio
async def test_list_thread_order_desc_reads_the_tail() -> None:
    """order="desc" flips both sort keys so LIMIT N is the newest N rows;
    anything else stays the ascending replay order."""
    ch = StubCH()
    ch.query_all_returns.append([_msg_row()])
    await list_thread("clickhouse", "ns", "claude-desktop", "t-1", order="desc", ch=ch)
    sql, params = ch.query_all_calls[0]
    assert "ORDER BY ts DESC, msg_id DESC" in sql
    assert "LIMIT {limit:UInt32}" in sql and params["limit"] == 500

    ch.query_all_returns.append([])
    await list_thread("clickhouse", "ns", "claude-desktop", "t-1", order="sideways", ch=ch)
    sql, _ = ch.query_all_calls[1]
    assert "ORDER BY ts ASC, msg_id ASC" in sql


@pytest.mark.asyncio
async def test_list_thread_cursor_and_run_id_filter() -> None:
    """Cursor and run_id thread through verbatim — conversation.list
    pagination relies on `(ts, msg_id) >` to skip the last seen row."""
    ch = StubCH()
    ch.query_all_returns.append([])
    after = (datetime(2026, 5, 16, 12, 0, tzinfo=UTC), "m-7")
    await list_thread(
        "clickhouse",
        "ns",
        "bot",
        "t-1",
        run_id="run-42",
        after=after,
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "(ts, msg_id) > (toDateTime64({ts:String}, 3, 'UTC'), {mid:String})" in sql
    assert "run_id = {run_id:String}" in sql
    assert params["mid"] == "m-7"
    # naive UTC, millisecond precision: what a basic-mode toDateTime64 parses on every build
    assert params["ts"] == "2026-05-16 12:00:00.000"
    assert params["run_id"] == "run-42"


@pytest.mark.asyncio
async def test_list_thread_no_cursor_no_run_id_omits_clauses() -> None:
    ch = StubCH()
    ch.query_all_returns.append([])
    await list_thread("clickhouse", "ns", "bot", "t-1", ch=ch)
    sql, _ = ch.query_all_calls[0]
    assert "(ts, msg_id) > " not in sql
    assert "run_id = " not in sql


@pytest.mark.asyncio
async def test_search_substring_uses_positioncaseinsensitive() -> None:
    """The lexical fallback emits CH's positionCaseInsensitive predicate
    and orders by recency (no cosineDistance — that's the vector path)."""
    ch = StubCH()
    ch.query_all_returns.append([_msg_row()])
    rows = await search_substring(
        "clickhouse",
        ["ns"],
        "bot",
        "deploy",
        scope=WHOLE_NAMESPACE,
        thread_id="t-1",
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "positionCaseInsensitive(content, {q:String}) > 0" in sql
    assert "ORDER BY ts DESC" in sql
    assert "thread_id = {thread_id:String}" in sql
    assert params["q"] == "deploy"
    assert params["thread_id"] == "t-1"
    assert len(rows) == 1 and isinstance(rows[0], MessageRow)
