"""A code search is a function of its inputs.

Every ranked read on the search path ends its ORDER BY in keys that identify the row, so
rows the score cannot tell apart come back in one order whatever parts hold them. The
vector-ranked reads keep the bare `ORDER BY distance` the codes-first read requires and
order the rows at one distance after the read. The lexical leg's order runs as SQL in
test_code_lexical_token_gate_sql.py and is held byte for byte in
test_code_lexical_statement.py.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from server.code.store.index import CodeRepo


class _CH:
    """Records each statement and answers it with the next scripted rows."""

    def __init__(self, *answers: list[dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers = list(answers)

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return list(self.answers.pop(0)) if self.answers else []


def _norm(sql: str) -> str:
    return " ".join(sql.split())


def _chunk(cid: str, path: str, line: int, distance: float) -> dict[str, Any]:
    return {"chunk_id": cid, "path": path, "start_line": line, "distance": distance}


@pytest.mark.asyncio
async def test_vector_rows_at_one_distance_come_in_file_order() -> None:
    """Chunks with one text have one vector, so they tie. The statement keeps the bare
    distance order and the rows come back by path, start line and chunk id."""
    rows = [
        _chunk("c", "src/a.py", 9, 0.25),
        _chunk("b", "src/a.py", 4, 0.25),
        _chunk("a", "src/a.py", 4, 0.25),
        _chunk("d", "lib/z.py", 80, 0.25),
        _chunk("e", "src/b.py", 1, 0.10),
        _chunk("f", "src/a.py", 1, 0.40),
    ]
    rng = random.Random(7)
    for _ in range(20):
        rng.shuffle(rows)
        ch = _CH(rows)
        found = await CodeRepo(ch).vector_search_chunks("o", "n", "m", [0.1, 0.2])  # type: ignore[arg-type]
        assert [r["chunk_id"] for r in found] == ["e", "d", "a", "b", "c", "f"]
        assert "ORDER BY distance ASC LIMIT {pool:UInt32}" in _norm(ch.calls[0][0])


@pytest.mark.asyncio
async def test_card_rows_at_one_distance_come_in_path_order() -> None:
    rows = [
        {"path": "src/b.py", "distance": 0.3},
        {"path": "src/a.py", "distance": 0.3},
        {"path": "src/c.py", "distance": 0.1},
    ]
    ch = _CH(rows)
    found = await CodeRepo(ch).vector_search_cards("o", "n", "m", [0.1, 0.2])  # type: ignore[arg-type]
    assert [r["path"] for r in found] == ["src/c.py", "src/a.py", "src/b.py"]
    assert "ORDER BY distance ASC LIMIT {pool:UInt32}" in _norm(ch.calls[0][0])


_Read = Callable[[CodeRepo], Awaitable[Any]]

# Each read with the ORDER BY its statement must carry: the last keys identify the row.
_CLOSED_ORDERS: list[tuple[str, _Read, str]] = [
    (
        "definition_chunks",
        lambda r: r.definition_chunks("o", "n", "m", [("src/a.py", "a::f")]),
        "ORDER BY path, scope, start_line, chunk_id LIMIT 1 BY path, scope",
    ),
    (
        "chunks_for_paths",
        lambda r: r.chunks_for_paths("o", "n", "m", ["src/a.py"]),
        "ORDER BY path, kind = 'class_skeleton' DESC, start_line, chunk_id LIMIT {per_path:UInt32}",
    ),
    (
        "best_chunks_for_paths",
        lambda r: r.best_chunks_for_paths("o", "n", "m", [0.1], ["src/a.py"]),
        "ORDER BY distance ASC, path, start_line, chunk_id LIMIT {per_path:UInt32} BY path",
    ),
    (
        "exact_rerank_by_ids",
        lambda r: r.exact_rerank_by_ids("o", "n", "m", [0.1], [("src/a.py", "c1")]),
        "ORDER BY distance ASC, path, start_line, chunk_id LIMIT {lim:UInt32}",
    ),
    (
        "track_path_chunks",
        lambda r: r.track_path_chunks("o", "n", "m", ["src/a.py"], ["merge"], [1.0], None),
        "ORDER BY path, start_line, chunk_id LIMIT {limit:UInt32}",
    ),
    (
        "pr_history_search",
        lambda r: r.pr_history_search("o", "n", ["merge"], [1.0]),
        "ORDER BY pr_score DESC, merged_at DESC, pr_number DESC LIMIT {pool:UInt32}",
    ),
    (
        "prs_linking_issue",
        lambda r: r.prs_linking_issue("o", "n", [7]),
        "ORDER BY merged_at DESC, pr_number DESC LIMIT {limit:UInt32}",
    ),
    (
        "symbol_names_for_paths",
        lambda r: r.symbol_names_for_paths("o", "n", ["src/a.py"]),
        "ORDER BY name, kind, path LIMIT {lim:UInt32}",
    ),
]


@pytest.mark.parametrize(
    ("read", "order"), [c[1:] for c in _CLOSED_ORDERS], ids=[c[0] for c in _CLOSED_ORDERS]
)
@pytest.mark.asyncio
async def test_a_ranked_read_closes_its_order_on_the_row(read: _Read, order: str) -> None:
    ch = _CH()
    await read(CodeRepo(ch))  # type: ignore[arg-type]
    assert order in _norm(ch.calls[0][0])


@pytest.mark.asyncio
async def test_symbol_lookup_reads_its_candidates_in_one_order() -> None:
    """The candidate keys come back in no order and the live read takes the first 4,000:
    they are sorted, and the live rows' order ends in the path, the line and the id."""
    candidates = [
        {"tier": "treesitter", "qualified_name": "m::f", "symbol_id": "s2"},
        {"tier": "scip", "qualified_name": "m::f", "symbol_id": "s9"},
        {"tier": "treesitter", "qualified_name": "m::f", "symbol_id": "s1"},
    ]
    ch = _CH(candidates)
    await CodeRepo(ch).symbols_by_name("o", "n", "f")  # type: ignore[arg-type]
    sql, params = ch.calls[1]
    assert params["keys"] == [
        ("scip", "m::f", "s9"),
        ("treesitter", "m::f", "s1"),
        ("treesitter", "m::f", "s2"),
    ]
    assert (
        "end_line - start_line DESC, qualified_name, path, start_line, symbol_id "
        "LIMIT {limit:UInt32}" in _norm(sql)
    )
