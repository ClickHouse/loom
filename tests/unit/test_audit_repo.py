"""Unit tests for server/insights/store/audit.py — mocked CH client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.insights.store.audit import (
    AuditRow,
    list_by_request_id,
)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []

    async def query_one(self, *args, **kwargs):
        raise AssertionError("not used")

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []


def _row() -> dict[str, Any]:
    return {
        "ts": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        "event_type": "memory.set",
        "org": "clickhouse",
        "namespace": "ns",
        "actor": "claude-desktop",
        "subject": '{"key":"v"}',
        "request_id": "req-1",
        "row_hash": "b" * 64,
    }


@pytest.mark.asyncio
async def test_list_by_request_id_threads_param_and_hydrates() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_row(), _row()])
    out = await list_by_request_id("req-1", ch=ch, limit=50)
    sql, params = ch.query_all_calls[0]
    assert "WHERE request_id = {rid:String}" in sql
    assert params["rid"] == "req-1"
    assert params["limit"] == 50
    assert len(out) == 2 and all(isinstance(r, AuditRow) for r in out)
    assert out[0].request_id == "req-1"


@pytest.mark.asyncio
async def test_fixed_string_row_hashes_read_back_as_hex_text() -> None:
    """row_hash is FixedString(64): the native client returns bytes, and a row
    written without one reads back as 64 NULs. Both come back as text."""
    ch = StubCH()
    ch.query_all_returns.append(
        [{**_row(), "row_hash": b"c" * 64}, {**_row(), "row_hash": b"\x00" * 64}]
    )
    out = await list_by_request_id("req-1", ch=ch)
    assert [r.row_hash for r in out] == ["c" * 64, ""]


@pytest.mark.asyncio
async def test_list_page_scopes_filters_and_keysets_below_the_cursor() -> None:
    from server.insights.store.audit import list_page

    ch = StubCH()
    ch.query_all_returns.append([{**_row(), "row_hash": b"d" * 64}])
    out = await list_page(
        "clickhouse",
        scope_where=" AND namespace = {ns:String}",
        scope_params={"ns": "ns"},
        event_type="memory.set",
        since=datetime(2026, 5, 1),
        before=(datetime(2026, 5, 16, 12, 0, 0, 250000, tzinfo=UTC), "req-9", None),
        limit=11,
        ch=ch,
    )
    sql, params = ch.query_all_calls[0]
    assert "WHERE org = {org:String}" in sql and "AND namespace = {ns:String}" in sql
    assert "AND event_type = {et:String}" in sql
    assert "(toDateTime64('2026-05-16 12:00:00.250', 3, 'UTC'), {c_rid:String})" in sql
    assert params == {
        "org": "clickhouse",
        "ns": "ns",
        "lim": 11,
        "et": "memory.set",
        "since": datetime(2026, 5, 1, tzinfo=UTC),  # a naive since is pinned to UTC
        "c_rid": "req-9",
    }
    assert [r.row_hash for r in out] == ["d" * 64]


@pytest.mark.asyncio
async def test_audit_list_pages_with_a_cursor_and_hex_hashes(monkeypatch) -> None:
    from server.auth import context as ctx
    from server.core.cursors import decode_cursor
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    ch = StubCH()
    older = {**_row(), "ts": datetime(2026, 5, 16, 11, 0, 0, tzinfo=UTC), "request_id": "req-0"}
    ch.query_all_returns.append([{**_row(), "row_hash": b"e" * 64}, older])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", limit=1))
    assert [r.row_hash for r in out.rows] == ["e" * 64]
    assert out.next_cursor is not None
    # The row hash breaks a tie between rows of one millisecond and one request id.
    assert decode_cursor(out.next_cursor)[1] == "req-1|" + "e" * 64
    _, params = ch.query_all_calls[0]
    assert params["lim"] == 2 and params["ns"] == "ns"


@pytest.mark.asyncio
async def test_the_next_page_keysets_on_the_row_hash_so_no_tied_row_is_skipped(
    monkeypatch,
) -> None:
    from server.auth import context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    # Three records of one write: one millisecond, one request id, three hashes.
    tied = [{**_row(), "row_hash": c * 64} for c in "fed"]
    ch = StubCH()
    ch.query_all_returns.append(tied[:2])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    first = await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", limit=1))
    assert first.next_cursor is not None
    ch.query_all_returns.append(tied[1:])
    await h.handle_audit_list(
        AuditListInput(org="clickhouse", namespace="ns", limit=1, cursor=first.next_cursor)
    )
    sql, params = ch.query_all_calls[1]
    assert "(ts, request_id, row_hash) <" in sql and "{c_hash:FixedString(64)}" in sql
    assert "ORDER BY ts DESC, request_id DESC, row_hash DESC" in sql
    assert (params["c_rid"], params["c_hash"]) == ("req-1", "f" * 64)


@pytest.mark.asyncio
async def test_a_cursor_issued_before_the_row_hash_still_pages(monkeypatch) -> None:
    from server.auth import context as ctx
    from server.core.cursors import encode_cursor
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    ch = StubCH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    old = encode_cursor(datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC), "req-1")
    await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", cursor=old))
    sql, params = ch.query_all_calls[0]
    assert "AND (ts, request_id) < (" in sql and "c_hash" not in params
    assert params["c_rid"] == "req-1"


def test_a_request_id_with_a_bar_keeps_its_row_hash() -> None:
    from server.core.cursors import encode_cursor
    from server.insights.app.audit import _decode_page_cursor

    ts = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    row_hash = "a" * 64
    assert _decode_page_cursor(encode_cursor(ts, "trace|1", row_hash)) == (ts, "trace|1", row_hash)
    # A cursor issued before the hash: the whole extra is the request id.
    assert _decode_page_cursor(encode_cursor(ts, "trace|1")) == (ts, "trace|1", None)
    # A page ending on a row with no hash still names its request id alone.
    assert _decode_page_cursor(encode_cursor(ts, "req-1", "")) == (ts, "req-1", "")
