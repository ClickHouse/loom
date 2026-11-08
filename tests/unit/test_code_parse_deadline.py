"""Every tree-sitter parse of repository text stops at `code_parse_timeout_seconds`."""

from __future__ import annotations

import ast
import asyncio
import pathlib
import time
from typing import Any

import pytest

from server.code.app.index.ingest_chunks import _chunks_for
from server.code.app.index.ingest_parse import parse_for_ingest
from server.settings import settings

_SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"

# Inputs some grammars parse in quadratic time: each takes tens of seconds unbounded
_SLOW = [
    ("slow.go", b"package x\nvar s = " + b"`" * 60_000),
    ("slow.js", b"a" + b"<" * 120_000),
    ("slow.sh", b"a=(" * 30_000),
]


@pytest.mark.parametrize(("path", "source"), _SLOW, ids=[p for p, _ in _SLOW])
def test_a_slow_parse_stops_at_the_deadline_and_the_file_is_chunked(
    monkeypatch: pytest.MonkeyPatch, path: str, source: bytes
) -> None:
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    start = time.monotonic()
    pf, err = parse_for_ingest(path, source)
    assert time.monotonic() - start < 5
    assert pf is not None and pf.symbols == [] and pf.source == source
    assert "ParseTimeout" in err


def test_a_file_parsed_in_time_keeps_its_symbols() -> None:
    source = (
        "package x\n" + "".join(f"func f{i}() {{ a := 'é' }}\n" for i in range(3000))
    ).encode()
    pf, err = parse_for_ingest("big.go", source)
    assert err == ""
    assert pf is not None and len(pf.symbols) == 3000


def test_a_host_file_whose_shell_parse_stops_keeps_its_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_host_islands", True)
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    source = b"build:\n\t" + b"a=(" * 30_000 + b"\n"
    pf, err = parse_for_ingest("Makefile", source)
    assert pf is not None and "ParseTimeout" in err
    chunks, _, facets = _chunks_for(pf, "Makefile", source, 4000)
    assert chunks and facets == []


def _is_parser(node: ast.expr) -> bool:
    """`parser`, or a call of a function named `...parser` (`_parser(grammar)`)."""
    if isinstance(node, ast.Name):
        return node.id == "parser"
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("parser")  # `self._parser`
    if isinstance(node, ast.Call):
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        return name.endswith("parser") or name == "Parser"
    return False


# Receivers of `.parse(` that are no tree-sitter parser: the bounded one, the definition
# reader over it, the hand-written lane parsers, the regex and Python parsers
_NOT_A_PARSER = {"bounded_parse", "Declarations", "_Parser", "_sre_parser", "ast"}


def _receiver(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    return node.id if isinstance(node, ast.Name) else ast.unparse(node)


def test_every_tree_sitter_parse_goes_through_the_deadline() -> None:
    """Outside `bounded_parse`, no `.parse(` is called on a parser, nor at all in the code
    plane's domain or a module that names tree-sitter but on `_NOT_A_PARSER`."""
    unbounded = []
    for f in _SERVER.rglob("*.py"):
        if f.name == "bounded_parse.py":
            continue
        text = f.read_text()
        scoped = "tree_sitter" in text or f.is_relative_to(_SERVER / "code" / "domain")
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "parse"
                and (
                    _is_parser(node.func.value)
                    or (scoped and _receiver(node.func.value) not in _NOT_A_PARSER)
                )
            ):
                unbounded.append(f"{f.relative_to(_SERVER.parent)}:{node.lineno}")
    assert unbounded == []


def test_a_header_whose_parse_stops_is_read_as_not_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_headers as ih
    from server.code.app.index.ingest_stats import FileIngestStats
    from server.code.domain.extract.treesitter import parse_cpp

    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    monkeypatch.setattr(ih, "_needs_header", lambda pf, hd: True)

    async def header_text(repo: Any, org: str, ns: str, path: str) -> tuple[str, bool]:
        return "'\\''" * 30_000, True

    async def none(*a: Any) -> list[str]:
        return []

    class Repo:
        async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
            return {"src/a.h"}

    monkeypatch.setattr(ih, "_header_text", header_text)
    monkeypatch.setattr(ih, "_present_includes", none)
    pf = parse_cpp("src/a.cpp", b"void f() { g(); }\n")
    stats = FileIngestStats(path="src/a.cpp")
    asyncio.run(ih.header_context(Repo(), "acme", "code", pf, "src/a.cpp", stats))
    assert ih._header_cache[("acme", "code", "src/a.h")] is None
    ih._header_cache.pop(("acme", "code", "src/a.h"))
