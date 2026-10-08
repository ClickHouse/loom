"""The symbol-plane chunk reads are CodeRepo statements: a symbol's body is the chunks
that carry its id, else the innermost chunk at its first line; a definition span is
covered when the overlapping chunks reach both ends."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.symbol.resolve import symbol_body
from server.code.app.symbol.verify import _body_complete
from server.code.store.index import CodeRepo


class _CH:
    def __init__(self, *, by_id: list[str], inner: str | None, span: tuple[int, int] | None):
        self.by_id, self.inner, self.span = by_id, inner, span
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.statements.append(" ".join(sql.split()))
        if "has(symbol_ids" in sql:
            assert params["sid"] == "s1" and params["limit"] == 3
            return [{"content": c} for c in self.by_id]
        assert "ORDER BY end_line - start_line ASC LIMIT 1" in sql
        return [{"content": self.inner}] if self.inner is not None else []

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.statements.append(" ".join(sql.split()))
        lo, hi = self.span if self.span else (None, None)
        return {"lo": lo, "hi": hi}


SYM = {"path": "src/a.cpp", "symbol_id": "s1", "start_line": 10, "end_line": 20}


def _body(ch: _CH) -> str:
    return asyncio.run(symbol_body(CodeRepo(ch), "o", "n", SYM))  # type: ignore[arg-type]


def test_the_body_is_the_chunks_carrying_the_symbol_id() -> None:
    ch = _CH(by_id=["a", "b"], inner="never", span=None)
    assert _body(ch) == "a\nb" and len(ch.statements) == 1


def test_a_stale_symbol_ids_array_falls_back_to_the_innermost_chunk() -> None:
    assert _body(_CH(by_id=[], inner="inner", span=None)) == "inner"
    assert _body(_CH(by_id=[], inner=None, span=None)) == ""


def test_the_definition_span_is_complete_only_when_the_chunks_reach_both_ends() -> None:
    def complete(span: tuple[int, int] | None) -> bool:
        repo = CodeRepo(_CH(by_id=[], inner=None, span=span))  # type: ignore[arg-type]
        return asyncio.run(_body_complete(repo, "o", "n", [SYM]))

    assert complete((5, 25)) is True
    assert complete((12, 25)) is False
    assert complete(None) is False
