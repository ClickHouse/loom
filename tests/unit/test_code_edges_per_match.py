"""edges_for_symbols(per_match=k) emits `LIMIT k BY <match column>` after the
ORDER BY and before the flat LIMIT, in the direction's own column, and omits
it when unset."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.store.index import CodeRepo


class _StubCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []


def _norm(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_per_match_caps_rows_per_matched_symbol_in_the_directions_column() -> None:
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.edges_for_symbols(
        "o", "n", ["s1", "s2"], direction="callees", limit=500, per_match=12
    )
    sql, params = ch.calls[0]
    n = _norm(sql)
    assert (
        "ORDER BY tier DESC, confidence DESC, path, line, to_qualified_name, from_qualified_name, "
        "to_symbol_id, from_symbol_id LIMIT {per_match:UInt32} BY from_symbol_id LIMIT {limit:UInt32}"
        in n
    )
    assert params["per_match"] == 12 and params["limit"] == 500

    await repo.edges_for_symbols("o", "n", ["s1"], direction="callers", per_match=3)
    sql, params = ch.calls[1]
    assert "LIMIT {per_match:UInt32} BY to_symbol_id" in _norm(sql)


@pytest.mark.asyncio
async def test_without_per_match_the_statement_is_unchanged() -> None:
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.edges_for_symbols("o", "n", ["s1"], direction="callers", limit=1000)
    sql, params = ch.calls[0]
    assert " BY " not in _norm(sql).split("ORDER BY")[1]
    assert "per_match" not in params


@pytest.mark.asyncio
async def test_the_order_is_total_so_a_capped_read_is_reproducible() -> None:
    """(tier, confidence, path, line) ties when one call site has several
    callees; the qualified names close the order so LIMIT and LIMIT BY are
    decided by the data, not by merge order — and the two symbol ids close
    it fully: template instantiations of one name at one
    call line tie on every column before them."""
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    await repo.edges_for_symbols("o", "n", ["s1"], direction="callees", per_match=3)
    order = _norm(ch.calls[0][0]).split("ORDER BY")[1].split("LIMIT")[0].strip()
    assert order == (
        "tier DESC, confidence DESC, path, line, to_qualified_name, from_qualified_name, "
        "to_symbol_id, from_symbol_id"
    )
