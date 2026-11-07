"""Unit tests for server/insights/store/audit.py — mocked CH client."""

from __future__ import annotations

import base64
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
async def test_the_cursor_reads_the_same_on_the_previous_build(monkeypatch) -> None:
    # During a rollout the next page may land on the previous build, whose decoder
    # (origin/main server/insights/app/audit.py) takes everything after the first
    # `|` as the request id. So this build still writes `ts|request_id`.
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
    assert decode_cursor(out.next_cursor) == (_row()["ts"], "req-1")
    _, params = ch.query_all_calls[0]
    assert params["lim"] == 2 and params["ns"] == "ns"


@pytest.mark.asyncio
async def test_a_page_never_ends_inside_one_writes_rows(monkeypatch) -> None:
    # Three records of one write share a millisecond and a request id, and (legacy
    # rows) no hash. A cursor of (ts, request_id) would skip the ones after it, so
    # the page takes them all, read again whole rather than after the last hash.
    from server.auth import context as ctx
    from server.core.cursors import decode_cursor
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    tied = [{**_row(), "row_hash": b"\x00" * 64, "subject": n} for n in "123"]
    older = {**_row(), "ts": datetime(2026, 5, 16, 11, 0, 0, tzinfo=UTC), "request_id": "req-0"}
    ch = StubCH()
    ch.query_all_returns.extend([tied[:2], tied, [older]])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", limit=1))
    assert [r.subject for r in out.rows] == ["1", "2", "3"]
    assert out.next_cursor is not None
    assert decode_cursor(out.next_cursor) == (_row()["ts"], "req-1")
    # The write is read by its (ts, request_id), with no hash comparison.
    sql, params = ch.query_all_calls[1]
    assert "AND ts = toDateTime64('2026-05-16 12:00:00.000', 3, 'UTC')" in sql
    assert "AND request_id = {w_rid:String}" in sql and "row_hash) <" not in sql
    assert params["w_rid"] == "req-1" and "c_hash" not in params
    # Then whether anything is older than the write.
    sql, params = ch.query_all_calls[2]
    assert "AND (ts, request_id) < (" in sql and params["lim"] == 1


@pytest.mark.asyncio
async def test_a_page_that_ends_with_a_whole_write_has_no_next_cursor(monkeypatch) -> None:
    from server.auth import context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    tied = [{**_row(), "subject": n} for n in "12"]
    ch = StubCH()
    ch.query_all_returns.extend([tied, tied, []])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", limit=1))
    assert [r.subject for r in out.rows] == ["1", "2"] and out.next_cursor is None


def _v2_cursor(ts: datetime, request_id: str, row_hash: str) -> str:
    """The second cursor format, which this build reads and does not write yet:
    base64 of `v2|<ts>|<row_hash>|<request_id>`."""
    raw = f"v2|{ts.isoformat()}|{row_hash}|{request_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


@pytest.mark.asyncio
async def test_a_versioned_cursor_keysets_on_the_row_hash(monkeypatch) -> None:
    from server.auth import context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    ch = StubCH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    cursor = _v2_cursor(_row()["ts"], "req-1", "f" * 64)
    await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", cursor=cursor))
    sql, params = ch.query_all_calls[0]
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


@pytest.mark.parametrize("request_id", ["req-1", "trace|1", "ends|", "|", ""])
def test_both_cursor_formats_keep_any_request_id(request_id: str) -> None:
    from server.core.cursors import encode_cursor
    from server.insights.app.audit import _decode_page_cursor

    ts = datetime(2026, 5, 16, 12, 0, tzinfo=UTC)
    # The previous build's `ts|request_id`: everything after the first bar is the id.
    assert _decode_page_cursor(encode_cursor(ts, request_id)) == (ts, request_id, None)
    for row_hash in ("a" * 64, ""):
        v2 = _v2_cursor(ts, request_id, row_hash)
        assert _decode_page_cursor(v2) == (ts, request_id, row_hash)


@pytest.mark.parametrize(
    "row_hash", ["a" * 63, "a" * 65, "é" * 64], ids=["short", "long", "multibyte"]
)
def test_a_v2_cursor_with_a_malformed_hash_is_a_validation_error(row_hash: str) -> None:
    # The hash binds as FixedString(64) bytes: a longer one would fail the query with a
    # 500. A legacy row's empty hash stays valid.
    from server.core.errors import ValidationError
    from server.insights.app.audit import _decode_page_cursor

    v2 = _v2_cursor(datetime(2026, 5, 16, 12, 0, tzinfo=UTC), "req-1", row_hash)
    with pytest.raises(ValidationError):
        _decode_page_cursor(v2)


@pytest.mark.asyncio
async def test_an_erasure_failure_written_before_the_upgrade_is_cut_on_read(monkeypatch) -> None:
    """An older build wrote the driver's text into `erasure.failed`'s `error` and
    `erasure.abandoned`'s `last_error`: `audit.list` shows the step and the kind."""
    import json

    from server.auth import context as ctx
    from server.insights.api.models_audit import AuditListInput
    from server.insights.app import audit as h

    text = "Code: 210. Connection refused (ch-internal-7:9440) ALTER TABLE loom.memory"
    failed = {"erasure_id": "e1", "error": f"memory.tombstone: ValueError: {text}"}
    abandoned = {"erasure_id": "e1", "last_error": f"RuntimeError: {text}", "attempts": 5}
    other = {"key": "v", "error": text}
    rows = [
        {**_row(), "event_type": "erasure.failed", "subject": json.dumps(failed)},
        {**_row(), "event_type": "erasure.abandoned", "subject": json.dumps(abandoned)},
        {**_row(), "event_type": "erasure.failed", "subject": '{"error": "RuntimeError"}'},
        {**_row(), "subject": json.dumps(other)},
    ]
    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    ch = StubCH()
    ch.query_all_returns.extend([rows, rows, []])
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = await h.handle_audit_list(AuditListInput(org="clickhouse", namespace="ns", limit=10))
    subjects = [json.loads(r.subject) for r in out.rows]
    assert subjects[0] == {**failed, "error": "memory.tombstone: ValueError"}
    assert subjects[1] == {**abandoned, "last_error": "RuntimeError"}
    assert out.rows[2].subject == '{"error": "RuntimeError"}'  # already cut: as stored
    assert subjects[3] == other  # another event's subject is never rewritten
