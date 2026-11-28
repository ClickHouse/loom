"""Rows a ranked read of the code search scores equally come back in one order.

Every ranked read on the search path ends its ORDER BY in keys that identify the row, so
rows the score cannot tell apart come back in one order whatever parts hold them: the
reads below run on clickhouse-local against a table written as one part per row, in two
orders. The vector-ranked reads keep the bare `ORDER BY distance` the codes-first read
requires and order the rows at one distance after the read; which rows at the read's
last distance come back is not fixed. The lexical leg's order runs as SQL in
test_code_lexical_token_gate_sql.py and is held byte for byte in
test_code_lexical_statement.py.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from server.code.store.index import CodeRepo
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl, view

local = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


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


def _row(cid: str, path: str, line: int, distance: float) -> dict[str, Any]:
    return {"chunk_id": cid, "path": path, "start_line": line, "distance": distance}


@pytest.mark.asyncio
async def test_vector_rows_at_one_distance_come_in_file_order() -> None:
    """Chunks with one text have one vector, so they tie. The statement keeps the bare
    distance order and the rows come back by path, start line and chunk id."""
    rows = [
        _row("c", "src/a.py", 9, 0.25),
        _row("b", "src/a.py", 4, 0.25),
        _row("a", "src/a.py", 4, 0.25),
        _row("d", "lib/z.py", 80, 0.25),
        _row("e", "src/b.py", 1, 0.10),
        _row("f", "src/a.py", 1, 0.40),
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


_Read = Callable[[CodeRepo], Awaitable[list[dict[str, Any]]]]


async def _in_both_layouts(
    setup: tuple[str, ...], inserts: list[str], read: _Read, *columns: str
) -> list[tuple[Any, ...]]:
    """The `columns` of the rows `read` returns from tables written as one part per
    insert, after checking that it returns the same rows, in the same order, when the
    parts are written in the reverse order."""

    async def rows(written: list[str]) -> list[tuple[Any, ...]]:
        found = await read(CodeRepo(LocalCH(*setup, *written)))  # type: ignore[arg-type]
        return [tuple(r[c] for c in columns) for r in found]

    first = await rows(inserts)
    assert first == await rows(inserts[::-1])
    return first


def _chunk_id(path: str, line: int, tag: str = "") -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{path}:{line}:{tag}"))


def _chunk(path: str, line: int, tag: str = "") -> str:
    """One function chunk of `path` that starts at `line`. Every chunk has the scope `f`,
    the lexeme `merge` and one vector, so no read can tell two of them apart by score."""
    return (
        "INSERT INTO loom.code_chunks (org, namespace, embedding_model, path, chunk_id, "
        "scope, kind, start_line, content, lexemes, embedding, embedding_dim) VALUES "
        f"('o', 'n', 'm', '{path}', '{_chunk_id(path, line, tag)}', 'f', 'function', {line}, "
        "'', 'merge', arrayResize([0.1], 3072, 0.1), 3072)"
    )


_TIED: list[tuple[str, int, str]] = [
    ("src/b.py", 40, ""),
    ("src/a.py", 90, ""),
    ("src/b.py", 1, ""),
    ("src/a.py", 7, "first"),
    ("src/a.py", 7, "second"),
    ("lib/z.py", 300, ""),
]
_CHUNKS = [_chunk(*c) for c in _TIED]
_KEYS = [(path, _chunk_id(path, line, tag)) for path, line, tag in _TIED]
_PATHS = ["lib/z.py", "src/a.py", "src/b.py"]
_QUERY = [0.1] * 3072
_FILE_ORDER = [
    ("lib/z.py", 300),
    ("src/a.py", 7),
    ("src/a.py", 7),
    ("src/a.py", 90),
    ("src/b.py", 1),
    ("src/b.py", 40),
]
_FIRST_OF_EACH_FILE = [("lib/z.py", 300), ("src/a.py", 7), ("src/b.py", 1)]

# Each read of the chunk table with the rows it returns for the six tied chunks.
_CHUNK_READS: list[tuple[str, _Read, list[tuple[str, int]]]] = [
    (
        "track_path_chunks",
        lambda r: r.track_path_chunks("o", "n", "m", _PATHS, ["merge"], [1.0], None),
        _FILE_ORDER,
    ),
    (
        "chunks_for_paths",
        lambda r: r.chunks_for_paths("o", "n", "m", _PATHS, per_path=3),
        _FILE_ORDER,
    ),
    (
        "best_chunks_for_paths",
        lambda r: r.best_chunks_for_paths("o", "n", "m", _QUERY, _PATHS),
        _FIRST_OF_EACH_FILE,
    ),
    (
        "exact_rerank_by_ids",
        lambda r: r.exact_rerank_by_ids("o", "n", "m", _QUERY, _KEYS),
        _FILE_ORDER,
    ),
    (
        "definition_chunks",
        lambda r: r.definition_chunks("o", "n", "m", [(path, "f") for path in _PATHS]),
        _FIRST_OF_EACH_FILE,
    ),
]


@local
@pytest.mark.parametrize(
    ("read", "expected"), [c[1:] for c in _CHUNK_READS], ids=[c[0] for c in _CHUNK_READS]
)
@pytest.mark.asyncio
async def test_a_chunk_read_returns_tied_chunks_in_file_order_whatever_part_holds_them(
    read: _Read, expected: list[tuple[str, int]]
) -> None:
    """Six chunks tie in every read. The rows come back by path and then by start line
    from both layouts, the two chunks that start on one line in the same order, and a
    read that keeps one chunk of a file keeps its first."""
    rows = await _in_both_layouts(
        ("SET enable_quantized_codec = 1", ddl("code_chunks")),
        _CHUNKS,
        read,
        "path",
        "start_line",
        "chunk_id",
    )
    assert [(path, line) for path, line, _ in rows] == expected


def _pr(number: int, merged_at: str) -> str:
    return (
        "INSERT INTO loom.code_pr_history (org, namespace, pr_number, title, author, files, "
        "merged_at, lexemes, linked_issues) VALUES "
        f"('o', 'n', {number}, 'Merge parts', 'dev', ['src/a.py'], '{merged_at}', 'merge', [7])"
    )


_PR_READS: list[tuple[str, _Read]] = [
    ("pr_history_search", lambda r: r.pr_history_search("o", "n", ["merge"], [1.0])),
    ("prs_linking_issue", lambda r: r.prs_linking_issue("o", "n", [7])),
]


@local
@pytest.mark.parametrize("read", [c[1] for c in _PR_READS], ids=[c[0] for c in _PR_READS])
@pytest.mark.asyncio
async def test_pull_requests_merged_in_one_second_come_higher_number_first(read: _Read) -> None:
    """Three pull requests match equally. The newest comes first, and of the two merged
    in one second the higher number."""
    prs = [
        _pr(11, "2024-05-01 10:00:00"),
        _pr(5, "2024-06-01 10:00:00"),
        _pr(12, "2024-05-01 10:00:00"),
    ]
    rows = await _in_both_layouts((ddl("code_pr_history"),), prs, read, "pr_number")
    assert rows == [(5,), (12,), (11,)]


def _symbol(name: str, qualified: str, symbol_id: str, path: str, line: int) -> str:
    """One five-line function `name` of `path` in the parser tier."""
    return (
        "INSERT INTO loom.code_symbols (org, namespace, tier, qualified_name, symbol_id, "
        "name, kind, path, start_line, end_line) VALUES "
        f"('o', 'n', 'treesitter', '{qualified}', '{symbol_id}', '{name}', 'function', "
        f"'{path}', {line}, {line + 4})"
    )


@local
@pytest.mark.asyncio
async def test_the_names_a_cut_keeps_are_the_first_in_name_order() -> None:
    """Five symbols and room for three: the first names, and both files' row of a name."""
    symbols = [
        _symbol("b", "a::b", "s1", "src/a.py", 1),
        _symbol("a", "b::a", "s2", "src/b.py", 1),
        _symbol("c", "a::c", "s3", "src/a.py", 9),
        _symbol("a", "a::a", "s4", "src/a.py", 20),
        _symbol("d", "b::d", "s5", "src/b.py", 9),
    ]
    rows = await _in_both_layouts(
        (ddl("code_symbols"),),
        symbols,
        lambda r: r.symbol_names_for_paths("o", "n", ["src/a.py", "src/b.py"], limit=3),
        "name",
        "path",
    )
    assert rows == [("a", "src/a.py"), ("a", "src/b.py"), ("b", "src/a.py")]


@local
@pytest.mark.asyncio
async def test_overloads_of_one_name_and_span_come_in_file_order() -> None:
    """Three definitions share a qualified name and a span, so the ranking keys leave
    them equal: they come by path and then by start line."""
    overloads = [
        _symbol("f", "m::f", "s1", "src/b.py", 5),
        _symbol("f", "m::f", "s2", "src/a.py", 9),
        _symbol("f", "m::f", "s3", "src/a.py", 2),
    ]
    rows = await _in_both_layouts(
        (ddl("code_symbols"), ddl("code_symbol_names"), view("code_symbol_names_mv")),
        overloads,
        lambda r: r.symbols_by_name("o", "n", "f"),
        "path",
        "start_line",
    )
    assert rows == [("src/a.py", 2), ("src/a.py", 9), ("src/b.py", 5)]


@pytest.mark.asyncio
async def test_symbol_lookup_reads_its_candidates_in_one_order() -> None:
    """The candidate keys come back in no order and the live read takes the first 4,000:
    they are sorted, the compiler tier's before the parser tier's."""
    candidates = [
        {"tier": "treesitter", "qualified_name": "m::f", "symbol_id": "s2"},
        {"tier": "scip", "qualified_name": "m::f", "symbol_id": "s9"},
        {"tier": "treesitter", "qualified_name": "m::f", "symbol_id": "s1"},
    ]
    ch = _CH(candidates)
    await CodeRepo(ch).symbols_by_name("o", "n", "f")  # type: ignore[arg-type]
    assert ch.calls[1][1]["keys"] == [
        ("scip", "m::f", "s9"),
        ("treesitter", "m::f", "s1"),
        ("treesitter", "m::f", "s2"),
    ]
