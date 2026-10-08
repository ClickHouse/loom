"""memory.recall — the raw turns, by text and by time."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_temporal import MemoryRecallInput
from server.memory.app import recall as h
from server.memory.domain import recall as svc


def test_query_tokens_drop_stop_words_dedupe_and_cap() -> None:
    assert svc.query_tokens("What did I say about the Chicago trip?") == ["say", "chicago", "trip"]
    assert svc.query_tokens("the the a of") == []
    assert svc.query_tokens("Six Flags six flags SIX") == ["six", "flags"]
    assert len(svc.query_tokens(" ".join(f"tok{i}" for i in range(80)))) == svc.MAX_QUERY_TOKENS
    # A single character is not a token; "x1" is.
    assert svc.query_tokens("x x1 y") == ["x1"]


def test_resolve_order_auto_follows_the_query() -> None:
    assert svc.resolve_order("auto", ["chicago"]) == "relevance"
    assert svc.resolve_order("auto", []) == "recent"
    assert svc.resolve_order("recent", ["chicago"]) == "recent"


def test_recall_sql_indexed_all_tokens_keyset_and_settings() -> None:
    sql, params = svc.build_recall_sql(
        svc.RecallFilter(tokens=["six", "flags"], match="all", agent="*"),
        order="recent",
        cursor=(datetime(2024, 1, 24, 10, 0), "0f0e0d0c-0000-0000-0000-000000000001"),
        limit=20,
    )
    assert "hasAllTokens(content_lc, {tokens:Array(String)})" in sql
    assert "(observed_at, evidence_id) < ({cur_ts:DateTime}, {cur_id:UUID})" in sql
    assert "ORDER BY observed_at DESC, evidence_id DESC" in sql
    # Projected copies are aliased away from the column names the keyset compares.
    assert "AS eid" in sql and "AS observed" in sql and "AS evidence_id" not in sql
    assert "query_plan_direct_read_from_text_index = 0" in sql
    assert "agent =" not in sql  # '*' reads every agent's turns in scope
    assert params["lim"] == 21 and params["off"] == 0
    assert params["tokens"] == ["six", "flags"]


def test_recall_sql_any_tokens_relevance_offset() -> None:
    sql, params = svc.build_recall_sql(
        svc.RecallFilter(
            tokens=["six"],
            match="any",
            agent="bot",
            user_id="u1",
            session_id="s9",
            role="user",
            since=datetime(2024, 1, 1),
            until=datetime(2024, 2, 1),
        ),
        order="relevance",
        offset=40,
        limit=20,
    )
    assert "hasAnyTokens(content_lc, {tokens:Array(String)})" in sql
    assert "position(content_lc, t) > 0" in sql
    assert "ORDER BY matched DESC, observed_at DESC" in sql
    for clause in (
        "agent = {agent:String}",
        "user_id = {uid:String}",
        "session_id = {sid:String}",
        "role = {role:String}",
        "observed_at >= {since:DateTime}",
        "observed_at <= {until:DateTime}",
    ):
        assert clause in sql, clause
    assert params["off"] == 40 and params["lim"] == 21


def test_count_sql_shares_the_filter() -> None:
    sql, params = svc.build_count_sql(svc.RecallFilter(tokens=["six"], role="assistant"))
    assert sql.strip().startswith("SELECT count() AS n")
    assert "role = {role:String}" in sql and params["role"] == "assistant"
    assert "hasAllTokens(content_lc" in sql


def test_page_rows_splits_limit_plus_one() -> None:
    rows = [{"i": i} for i in range(21)]
    page, more = svc.page_rows(rows, 20)
    assert len(page) == 20 and more is True
    page, more = svc.page_rows(rows[:5], 20)
    assert len(page) == 5 and more is False


class _CH:
    def __init__(self, rows: list[dict[str, Any]], total: int) -> None:
        self.rows, self.total = rows, total
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return {"n": self.total}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return self.rows


def _turn(i: int, ts: str, content: str, role: str = "user") -> dict[str, Any]:
    return {
        "eid": f"00000000-0000-0000-0000-{i:012d}",
        "session_id": "s1",
        "role": role,
        "actor": "",
        "observed": ts,
        "content": content,
        "matched": 2,
    }


def _inp(**kw: Any) -> MemoryRecallInput:
    data: dict[str, Any] = {"org": "o", "namespace": "n", "agent": "*"}
    data.update(kw)
    return MemoryRecallInput(**data)


@pytest.mark.asyncio
async def test_recall_refuses_an_unscoped_read(monkeypatch) -> None:
    monkeypatch.setattr(h, "get_client", lambda: _CH([], 0))
    with pytest.raises(HTTPException) as ei:
        await h.handle_memory_recall(_inp(query="the of and"))
    assert ei.value.status_code == 400
    assert ei.value.detail["code"] == "RECALL_REQUIRES_FILTER"


@pytest.mark.asyncio
async def test_recall_pages_newest_first_with_a_cursor_and_reports_totals(monkeypatch) -> None:
    rows = [_turn(i, f"2024-01-{24 - i:02d} 10:00:00", f"turn {i} six flags") for i in range(3)]
    ch = _CH(rows, total=124)
    monkeypatch.setattr(h, "get_client", lambda: ch)
    monkeypatch.setattr(h, "_apply_read_scope_for_tests", None, raising=False)

    out = await h.handle_memory_recall(_inp(query="six flags", order="recent", top_k=2))
    assert out.order == "recent"
    assert out.returned == 2 and out.total_matched == 124 and out.has_more is True
    assert out.next_cursor and out.next_offset == -1
    assert out.turns[0].content == "turn 0 six flags" and out.turns[0].matched_tokens == 2
    assert out.query_tokens == ["six", "flags"]
    # org / namespace ride every statement; the page asks for limit+1.
    page_sql, page_params = ch.calls[0]
    assert page_params["org"] == "o" and page_params["ns"] == "n" and page_params["lim"] == 3
    assert "LIMIT 1 BY evidence_id" in page_sql

    # The cursor decodes to the last row shown and feeds the next page.
    from server.core.cursors import decode_cursor

    ts, eid = decode_cursor(out.next_cursor)
    assert ts == datetime(2024, 1, 23, 10, 0) and eid.endswith("000000000001")
    await h.handle_memory_recall(
        _inp(query="six flags", order="recent", top_k=2, cursor=out.next_cursor)
    )
    assert "cur_ts" in ch.calls[-2][1]


@pytest.mark.asyncio
async def test_recall_relevance_pages_by_offset_and_truncates_long_turns(monkeypatch) -> None:
    rows = [_turn(1, "2024-01-24 10:00:00", "x" * 1000), _turn(2, "2024-01-23 10:00:00", "short")]
    ch = _CH(rows, total=2)
    monkeypatch.setattr(h, "get_client", lambda: ch)

    out = await h.handle_memory_recall(_inp(query="anything here", top_k=1, max_chars=100))
    assert out.order == "relevance"
    assert out.has_more is True and out.next_offset == 1 and out.next_cursor == ""
    assert out.turns[0].truncated is True and len(out.turns[0].content) == 100
    assert out.tokens_returned == 25
    page_sql = ch.calls[0][0]
    assert "content_lc" in page_sql


@pytest.mark.asyncio
async def test_recall_time_window_without_a_query_is_newest_first(monkeypatch) -> None:
    ch = _CH([], total=0)
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = await h.handle_memory_recall(
        _inp(since=datetime(2024, 1, 1), until=datetime(2024, 1, 2), thread="s1", role="user")
    )
    assert out.order == "recent" and out.returned == 0 and out.has_more is False
    sql, params = ch.calls[0]
    assert "hasAllTokens" not in sql and "hasAnyTokens" not in sql
    assert params["sid"] == "s1" and params["role"] == "user"
    assert "toUInt32(0) AS matched" in sql


@pytest.mark.asyncio
async def test_recall_rejects_a_foreign_cursor(monkeypatch) -> None:
    monkeypatch.setattr(h, "get_client", lambda: _CH([], 0))
    with pytest.raises(HTTPException) as ei:
        await h.handle_memory_recall(
            _inp(query="six flags", order="recent", cursor="not-a-cursor!")
        )
    assert ei.value.detail["code"] == "BAD_CURSOR"
