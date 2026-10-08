"""The symbol-names read dedupes versions instead of FINAL."""

from __future__ import annotations

import asyncio

from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_all(self, sql, params=None):
        self.sql.append(sql)
        return []


def test_symbol_names_read_has_no_final_and_dedupes_by_version() -> None:
    ch = _CH()
    asyncio.run(CodeRepo(ch).symbol_names_for_paths("o", "n", ["src/a.cpp"]))  # type: ignore[arg-type]
    sql = ch.sql[0]
    assert "FINAL" not in sql and "GROUP BY tier, qualified_name, symbol_id" in sql
    assert "argMax(tombstoned_at IS NULL, version) AS is_live" in sql and "WHERE is_live = 1" in sql
