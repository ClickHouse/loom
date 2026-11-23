"""A definition body is complete or disclosed as elided on HTTP and MCP."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.api.models_symbol import CodeSymbolInput
from server.code.app.symbol.symbol import _symbol_bodies, _unit_with_body
from server.code.domain.symbol_body import complete_source_span
from server.transport.code_lean import lean_code

FULL = "def f():\n" + "    # source line\n" * 7 + "    return 1\n"
PREFIX = "".join(FULL.splitlines(keepends=True)[:4])
SYM = {
    "symbol_id": "compiler-f",
    "tier_ids": ["compiler-f", "parser-f"],
    "qualified_name": "module::f",
    "name": "f",
    "kind": "function",
    "path": "module.py",
    "start_line": 10,
    "end_line": 18,
    "tier": "scip",
}


def _row(text: str, start: int, end: int, **extra: Any) -> dict[str, Any]:
    return {
        "content": text,
        "start_line": start,
        "end_line": end,
        "kind": "function",
        "embedding_model": "model",
        "indexed_commit": "a" * 40,
        **extra,
    }


class _Repo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.reads = 0

    async def source_chunks_for_span(self, *args: Any) -> list[dict[str, Any]]:
        self.reads += 1
        return self.rows


async def _unit(repo: _Repo, *, row: dict[str, Any] | None = None, include_body: bool = True):
    symbol = row or SYM
    parts = await _symbol_bodies(
        repo, "org", "code-repo", CodeSymbolInput(include_body=include_body), [symbol]
    )
    return _unit_with_body(symbol, parts[0], {})


@pytest.mark.parametrize(
    "rows",
    [
        [_row(PREFIX, 10, 13)],
        [_row("".join(FULL.splitlines(keepends=True)[4:]), 14, 18)],
    ],
    ids=["missing-tail", "missing-start"],
)
async def test_a_partial_chunk_cannot_claim_the_whole_definition(
    rows: list[dict[str, Any]],
) -> None:
    unit = await _unit(_Repo(rows))
    assert unit.body == ""
    assert unit.elided and unit.granularity == "card" and unit.next
    wire = unit.model_dump()
    assert wire["elided"]
    mcp = lean_code("code.symbol", {"definitions": [wire]})
    assert mcp["definitions"][0]["elided"]
    assert mcp["definitions"][0]["next"]


async def test_the_retained_span_replaces_a_partial_header() -> None:
    unit = await _unit(_Repo([_row(PREFIX, 10, 13), _row(FULL, 10, 18)]))
    assert unit.body == FULL
    assert not unit.elided


async def test_one_complete_chunk_remains_a_complete_definition() -> None:
    unit = await _unit(_Repo([_row(FULL, 10, 18)]))
    assert unit.body == FULL and not unit.elided


async def test_a_large_retained_definition_has_no_implicit_character_limit() -> None:
    text = "def f():\n    return '" + "x" * 12000 + "'\n"
    unit = await _unit(_Repo([_row(text, 10, 11)]), row={**SYM, "end_line": 11})
    assert unit.body == text and not unit.elided
    mcp = lean_code("code.symbol", {"definitions": [unit.model_dump()]})
    assert mcp["definitions"][0]["body"] == text
    assert not mcp["definitions"][0].get("elided")


async def test_a_deliberate_pointer_request_reads_no_source() -> None:
    repo = _Repo([_row(FULL, 10, 18)])
    unit = await _unit(repo, include_body=False)
    assert unit.body == "" and repo.reads == 0


@pytest.mark.parametrize(
    ("reader_failed", "budget"),
    [(False, 2500), (True, 2500), (False, 200), (False, 0), (True, 0)],
    ids=[
        "missing-skeleton",
        "failed-reader",
        "paged-outline",
        "unlimited-outline",
        "unlimited-failed-reader",
    ],
)
async def test_a_large_container_without_source_discloses_omission_and_keeps_outline(
    reader_failed: bool,
    budget: int,
) -> None:
    class ContainerRepo(_Repo):
        async def class_skeleton_chunk(self, *args: Any) -> None:
            if reader_failed:
                raise RuntimeError("skeleton reader unavailable")

        async def outline_for_path(self, *args: Any) -> list[dict[str, Any]]:
            return [
                {
                    **SYM,
                    "qualified_name": f"module::f::member_{i}",
                    "start_line": 12 + i * 20,
                    "end_line": 18 + i * 20,
                }
                for i in range(8)
            ]

    symbol = {**SYM, "kind": "class", "end_line": 309}
    parts = await _symbol_bodies(
        ContainerRepo([]), "org", "code-repo", CodeSymbolInput(token_budget=budget), [symbol]
    )
    unit = _unit_with_body(symbol, parts[0], {})
    assert unit.body == "" and unit.elided and unit.granularity == "card"
    if budget == 200:
        assert unit.next == "code.outline path=module.py"
    else:
        assert unit.next == "Read module.py offset=10 limit=300"
    if budget in (0, 2500):
        assert len(unit.outline) == (0 if reader_failed else 8)
    wire = lean_code("code.symbol", {"definitions": [unit.model_dump()]})["definitions"][0]
    assert wire["elided"] and wire["next"] == unit.next
    if unit.outline:
        assert wire["outline"] == unit.outline


def test_source_pieces_are_placed_without_repeating_overlap_or_doc_prefix() -> None:
    lines = FULL.splitlines(keepends=True)
    rows = [
        _row("Doc comment\n" + "".join(lines[:5]), 10, 14),
        _row("# … continued from def f():\n" + "".join(lines[4:]), 14, 18),
    ]
    assert complete_source_span(rows, 10, 18, max_chars=12000) == FULL


@pytest.mark.parametrize(
    "rows",
    [
        [_row(PREFIX, 10, 13), _row("    return 1\n", 18, 18)],
        [_row(FULL, 10, 18), _row("    return 2\n", 18, 18)],
        [_row(FULL, 10, 18, kind="class_skeleton")],
        [_row("short\n", 10, 18)],
        [_row("def f(): # … line continues\n", 10, 10), _row(FULL, 10, 18)],
        [_row("… continued at line 19\n", 10, 10), _row(FULL, 10, 18)],
    ],
    ids=["gap", "conflict", "skeleton", "short-content", "cut-line", "cut-window"],
)
def test_incomplete_or_ambiguous_source_is_not_invented(rows: list[dict[str, Any]]) -> None:
    assert complete_source_span(rows, 10, 18, max_chars=12000) == ""


async def test_a_capped_source_read_is_disclosed_as_elided() -> None:
    unit = await _unit(_Repo([_row(FULL, 10, 18)] * 401))
    assert unit.elided and not unit.body


async def test_a_compiler_declaration_line_cannot_claim_a_long_function_body() -> None:
    unit = await _unit(_Repo([_row(FULL, 10, 18)]), row={**SYM, "end_line": 10})
    assert unit.elided and not unit.body


async def test_a_real_single_line_function_can_be_served_whole() -> None:
    text = "def f(): return 1\n"
    unit = await _unit(_Repo([_row(text, 10, 10)]), row={**SYM, "end_line": 10})
    assert unit.body == text and not unit.elided


@pytest.mark.parametrize("separator", ["\u0085", "\u2028"], ids=["next-line", "line-separator"])
async def test_unicode_inside_a_string_does_not_change_physical_source_lines(
    separator: str,
) -> None:
    text = f'def f(): return "a{separator}b"\n'
    compile(text, "module.py", "exec")
    unit = await _unit(_Repo([_row(text, 10, 10)]), row={**SYM, "end_line": 10})
    assert unit.body == text and not unit.elided


def test_retained_unchanged_pieces_can_have_different_introduction_commits() -> None:
    lines = FULL.splitlines(keepends=True)
    rows = [
        _row("".join(lines[:4]), 10, 13, indexed_commit="a" * 40),
        _row("".join(lines[4:]), 14, 18, indexed_commit="b" * 40),
    ]
    assert complete_source_span(rows, 10, 18, max_chars=12000) == FULL


async def test_unlimited_large_class_returns_retained_source_instead_of_a_skeleton() -> None:
    text = "class Large:\n" + "    member = 1\n" * 300
    symbol = {**SYM, "kind": "class", "start_line": 1, "end_line": 301}
    unit = await _unit(_Repo([_row(text, 1, 301)]), row=symbol)
    assert unit.body == text and not unit.elided and unit.granularity == "unit"
