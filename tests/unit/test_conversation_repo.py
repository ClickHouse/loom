"""Unit tests for server/knowledge/store/conversations.py — mocked CH client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.knowledge.store.conversations import (
    MessageInsert,
    MessageRow,
    append,
    list_thread,
    search_substring,
)


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
    assert settings.get("async_insert") == 0


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
