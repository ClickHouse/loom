"""chunk_hashes_for_symbols: an id the chunk tags miss (a compiler-tier id)
resolves to the tightest live chunk containing its definition line, read for
every pending row in one statement, not one statement per row."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store.index import CodeRepo


class _CH:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        if "arrayJoin(symbol_ids)" in sql:
            return [{"sid": "tagged", "h": b"aa" + b"\x00" * 2}]
        return [c for c in self.chunks if c["path"] in (params or {}).get("paths", [])]


def _chunk(path: str, start: int, end: int, h: str) -> dict[str, Any]:
    return {"path": path, "start_line": start, "end_line": end, "content_hash": h.encode()}


def test_pending_rows_resolve_to_the_tightest_containing_chunk_in_one_read() -> None:
    ch = _CH(
        [
            _chunk("a.cpp", 1, 200, "file"),
            _chunk("a.cpp", 10, 40, "class"),
            _chunk("a.cpp", 12, 20, "method"),
            _chunk("a.cpp", 30, 38, "other"),
            _chunk("b.cpp", 5, 9, "b-fn"),
            _chunk("b.cpp", 5, 9, "b-tie-later"),  # same span, same start: first kept
            _chunk("b.cpp", 4, 8, "b-earlier"),  # same span, earlier start wins
        ]
    )
    rows = [
        {"symbol_id": "tagged", "path": "a.cpp", "start_line": 12},
        {"symbol_id": "s-method", "path": "a.cpp", "start_line": 15},
        {"symbol_id": "s-class", "path": "a.cpp", "start_line": 25},
        {"symbol_id": "s-b", "path": "b.cpp", "start_line": 6},
        {"symbol_id": "s-none", "path": "c.cpp", "start_line": 3},
    ]
    out = asyncio.run(
        CodeRepo(ch).chunk_hashes_for_symbols(  # type: ignore[arg-type]
            "o", "n", [r["symbol_id"] for r in rows], rows=rows
        )
    )
    assert out == {
        "tagged": "aa",
        "s-method": "method",
        "s-class": "class",
        "s-b": "b-earlier",
    }
    assert len(ch.calls) == 2, "the tag read and one containing-chunk read"
    sql, params = ch.calls[1]
    assert "path IN {paths:Array(String)}" in sql
    assert params["paths"] == ["a.cpp", "b.cpp", "c.cpp"] and params["max_line"] == 25


def test_no_pending_rows_reads_nothing_more() -> None:
    ch = _CH([])
    out = asyncio.run(CodeRepo(ch).chunk_hashes_for_symbols("o", "n", ["tagged"]))  # type: ignore[arg-type]
    assert out == {"tagged": "aa"} and len(ch.calls) == 1
