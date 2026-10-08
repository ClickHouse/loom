"""`symbols_by_ids` discovers its keys through the id-keyed
side table and reads the main table by full sort key, returning the live,
scip-preferred rows."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.store import index as m


class _CH:
    def __init__(self, side_rows: list[dict[str, Any]], main_rows: list[dict[str, Any]]) -> None:
        self.side_rows, self.main_rows = side_rows, main_rows
        self.statements: list[str] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.statements.append(" ".join(sql.split()))
        if "FROM loom.code_symbol_ids" in sql:
            ids = set((params or {}).get("ids") or [])
            return [r for r in self.side_rows if r["symbol_id"] in ids]
        assert "FROM loom.code_symbols FINAL" in sql
        keys = {tuple(k) for k in (params or {})["keys"]}
        return [
            r for r in self.main_rows if (r["tier"], r["qualified_name"], r["symbol_id"]) in keys
        ]


def _repo(ch: _CH) -> m.CodeRepo:
    repo = m.CodeRepo.__new__(m.CodeRepo)
    repo._ch = ch  # type: ignore[attr-defined]
    return repo


MAIN = [
    {"tier": "treesitter", "qualified_name": "a.b::F", "symbol_id": "s1", "kind": "function"},
    {"tier": "scip", "qualified_name": "a.b::F", "symbol_id": "s1", "kind": "function"},
    {"tier": "treesitter", "qualified_name": "a.b::G", "symbol_id": "s2", "kind": "function"},
]
SIDE = [
    {"tier": "treesitter", "qualified_name": "a.b::F", "symbol_id": "s1"},
    {"tier": "scip", "qualified_name": "a.b::F", "symbol_id": "s1"},
    {"tier": "treesitter", "qualified_name": "a.b::G", "symbol_id": "s2"},
    {"tier": "treesitter", "qualified_name": "a.b::H", "symbol_id": "s3"},
]


@pytest.mark.asyncio
async def test_keys_come_from_the_side_table_and_rows_by_full_key() -> None:
    ch = _CH(SIDE, MAIN)
    rows = await _repo(ch).symbols_by_ids("o", "n", ["s1", "s2", "missing"])
    assert {(r["tier"], r["symbol_id"]) for r in rows} == {("scip", "s1"), ("treesitter", "s2")}
    main_reads = [s for s in ch.statements if "FROM loom.code_symbols FINAL" in s]
    assert len(main_reads) == 1 and "(tier, qualified_name, symbol_id) IN" in main_reads[0]
    assert not any("symbol_id IN" in s and "code_symbols FINAL" in s for s in ch.statements)


@pytest.mark.asyncio
async def test_no_keys_means_no_main_read() -> None:
    ch = _CH(SIDE, MAIN)
    repo = _repo(ch)
    assert await repo.symbols_by_ids("o", "n", ["nope"]) == []
    assert not [s for s in ch.statements if "FROM loom.code_symbols FINAL" in s]
    assert await repo.symbols_by_ids("o", "n", []) == []
