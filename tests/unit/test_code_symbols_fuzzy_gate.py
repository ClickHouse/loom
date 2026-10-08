"""The fuzzy symbol leg gates with `lower(name) LIKE` so the
n-gram index on lower(name) prunes granules; the
`positionCaseInsensitive` predicate keeps the exact semantics."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.store.index import CodeRepo, like_contains


def test_like_contains_escapes_every_wildcard_so_the_gate_is_a_substring_test() -> None:
    assert like_contains("quo") == "%quo%"
    assert like_contains("50%_x\\y") == "%50\\%\\_x\\\\y%"
    assert like_contains("") == "%%"


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, params or {}))
        return []


def test_symbols_fuzzy_gates_with_lower_like_and_keeps_the_position_predicate() -> None:
    ch = _CH()
    repo = CodeRepo.__new__(CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]
    asyncio.run(repo.symbols_fuzzy("o", "code-x", "Enforced", limit=1))
    sql, params = ch.calls[0]
    assert "AND lower(name) LIKE {like:String}" in sql
    assert "AND positionCaseInsensitive(name, {needle:String}) > 0" in sql
    assert params["like"] == "%enforced%" and params["needle"] == "Enforced"
    assert sql.index("LIKE {like:String}") < sql.index("positionCaseInsensitive")
