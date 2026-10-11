"""context.offload / context.fetch: pure helpers, handlers over a fake CH,
and registration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from server.memory.api.models_context_offload import ContextFetchInput, ContextOffloadInput
from server.memory.api.ops_context_offload import context_fetch, context_offload
from server.memory.app import context_offload as h

LOG = "\n".join(f"line {i}: {'ERROR disk full' if i % 50 == 0 else 'ok'}" for i in range(1, 301))
IDENT = {"org": "o", "namespace": "ns", "agent": "a"}


def _rx(pattern: str):
    from server.core.caller_regex import compile_caller_regex

    return compile_caller_regex(pattern, case_sensitive=False)


def test_content_key_is_deterministic_and_summary_flattens() -> None:
    sha, key = h.content_key("hello\nworld")
    assert key == f"blob/{sha[:20]}" and h.content_key("hello\nworld") == (sha, key)
    assert h.default_summary("  a\n\n b   c " * 100, chars=20).endswith("…")
    assert h.default_summary("short") == "short"
    assert "context.fetch blob_key=blob/x" in h.render_pointer("blob/x", 1234, 12, "s")


def test_slice_and_grep_paginate() -> None:
    piece, nxt = h.slice_content("abcdef", 0, 4)
    assert (piece, nxt) == ("abcd", 4)
    assert h.slice_content("abcdef", 4, 4) == ("ef", -1)
    text, matched, nxt = h.grep_content(LOG, _rx("error"), offset=0, limit=60)
    assert matched == 6 and text.startswith("50: line 50: ERROR") and nxt == 2
    text2, _, nxt2 = h.grep_content(LOG, _rx("error"), offset=nxt, limit=10_000, context_lines=1)
    assert text2.startswith("149- line 149: ok\n150: line 150: ERROR") and nxt2 == -1


class _FakeCH:
    def __init__(self, row=None):
        self.inserted: list[tuple[str, list[dict]]] = []
        self.row = row

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))

    async def query_one(self, sql, params=None):
        assert "expires_at > now()" in sql, "TTL is retention, reads must filter"
        return self.row


def test_offload_writes_one_row_and_returns_a_pointer(monkeypatch) -> None:
    ch = _FakeCH()
    monkeypatch.setattr(h, "get_client", lambda: ch)
    out = asyncio.run(h.handle_context_offload(ContextOffloadInput(**IDENT, content=LOG)))
    table, rows = ch.inserted[0]
    assert table == "loom.context_blobs" and rows[0]["blob_key"] == out.blob_key
    assert rows[0]["content"] == LOG and rows[0]["content_bytes"] == len(LOG.encode())
    assert out.blob_key.startswith("blob/") and out.content_chars == len(LOG)
    assert out.expires_at - rows[0]["created_at"] == timedelta(days=7)
    assert out.pointer.startswith("[offloaded") and out.summary.startswith("line 1: ok")
    # caller key + summary win; oversize is a 413
    out2 = asyncio.run(
        h.handle_context_offload(
            ContextOffloadInput(**IDENT, content="x", blob_key="tool/run-7", summary="run 7 log")
        )
    )
    assert out2.blob_key == "tool/run-7" and out2.summary == "run 7 log"
    monkeypatch.setattr(h.settings, "context_offload_max_bytes", 10)
    try:
        asyncio.run(h.handle_context_offload(ContextOffloadInput(**IDENT, content=LOG)))
    except Exception as e:  # noqa: BLE001
        assert getattr(e, "status_code", None) == 413
    else:
        raise AssertionError("expected 413")


def test_fetch_slices_greps_and_reports_missing(monkeypatch) -> None:
    row = {
        "content": LOG,
        "content_type": "text/plain",
        "summary": "s",
        "expires_at": datetime(2026, 9, 18),
    }
    monkeypatch.setattr(h, "get_client", lambda: _FakeCH(row))
    out = asyncio.run(
        h.handle_context_fetch(ContextFetchInput(**IDENT, blob_key="blob/k", limit=100))
    )
    assert out.found and len(out.content) == 100 and out.next_offset == 100
    assert out.total_lines == 300 and out.total_chars == len(LOG)
    g = asyncio.run(
        h.handle_context_fetch(
            ContextFetchInput(**IDENT, blob_key="blob/k", pattern="disk full", limit=10_000)
        )
    )
    assert g.matched_lines == 6 and g.content.count("\n") == 5 and g.next_offset == -1
    monkeypatch.setattr(h, "get_client", lambda: _FakeCH(None))
    miss = asyncio.run(h.handle_context_fetch(ContextFetchInput(**IDENT, blob_key="blob/gone")))
    assert miss.found is False and miss.content == ""


def test_fetch_refuses_an_invalid_pattern_before_reading(monkeypatch) -> None:
    import pytest

    from server.core.errors import ValidationError

    def _no_read():
        raise AssertionError("an invalid pattern must not reach ClickHouse")

    monkeypatch.setattr(h, "get_client", _no_read)
    with pytest.raises(ValidationError) as err:
        asyncio.run(
            h.handle_context_fetch(
                ContextFetchInput(**IDENT, blob_key="blob/k", pattern="(unclosed")
            )
        )
    assert err.value.status_code == 422 and "regular expression" in str(err.value)


def test_operations_registered_with_roles_and_descriptions() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    assert OPERATIONS_BY_NAME["context.offload"] is context_offload
    assert OPERATIONS_BY_NAME["context.fetch"] is context_fetch
    assert (
        context_offload.required_role == Role.WRITER and context_fetch.required_role == Role.VIEWER
    )
    assert "context.offload" in TOOL_DESCRIPTIONS and "context.fetch" in TOOL_DESCRIPTIONS


def test_grep_content_runs_a_catastrophic_pattern_in_linear_time() -> None:
    import time

    started = time.monotonic()
    text, matched, _ = h.grep_content("a" * 50_000 + "!", _rx("(a+)+$"), offset=0, limit=100)
    assert matched == 0 and text == ""
    assert time.monotonic() - started < 2.0


def test_fetch_refuses_a_backreference_naming_re2(monkeypatch) -> None:
    import pytest

    from server.core.errors import ValidationError

    def _no_read():
        raise AssertionError("an invalid pattern must not reach ClickHouse")

    monkeypatch.setattr(h, "get_client", _no_read)
    with pytest.raises(ValidationError, match="RE2") as err:
        asyncio.run(
            h.handle_context_fetch(ContextFetchInput(**IDENT, blob_key="blob/k", pattern=r"(a)\1"))
        )
    assert err.value.extra["code"] == "BAD_PATTERN"


def test_grep_content_scans_at_most_the_offload_cap(monkeypatch) -> None:
    monkeypatch.setattr(h.settings, "context_offload_max_bytes", 10)
    _, matched, _ = h.grep_content("hit\n" * 10, _rx("hit"), offset=0, limit=1000)
    assert matched == 2  # only "hit\nhit\nhi" is scanned
    # the cap is bytes: four 3-byte characters are 12 bytes, past a 10-byte cap
    _, matched, _ = h.grep_content("€€€€\nhit", _rx("hit"), offset=0, limit=1000)
    assert matched == 0
