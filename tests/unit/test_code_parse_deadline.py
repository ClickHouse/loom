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

    def again(path: str, source: bytes) -> None:
        raise AssertionError("the chunk step parsed the shell again")

    monkeypatch.setattr("server.code.app.index.ingest_chunks.host_units_of", again)
    chunks, _, facets = _chunks_for(pf, "Makefile", source, 4000, None, err)
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
    """Outside `bounded_parse`, no `.parse(` is called on a parser; and in the code plane's
    domain, or in any module that names tree-sitter, no `.parse(` at all except on a
    receiver in `_NOT_A_PARSER`. A parser is known by its name (`parser`, `_parser(...)`),
    so one held under another name outside those modules is not caught."""
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
    monkeypatch.setattr(ih, "_header_cache", {})

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


def test_two_threads_never_parse_one_grammar_at_once() -> None:
    import threading

    import tree_sitter
    import tree_sitter_go

    from server.code.domain.extract import bounded_parse

    active, most = 0, 0
    count = threading.Lock()

    class Watched:
        """A Go parser that records how many threads are inside it."""

        def __init__(self) -> None:
            self.language = tree_sitter.Language(tree_sitter_go.language())
            self._parser = tree_sitter.Parser(self.language)

        def parse(self, read: Any) -> Any:
            nonlocal active, most
            with count:
                active += 1
                most = max(most, active)
            time.sleep(0.02)
            try:
                return self._parser.parse(read)
            finally:
                with count:
                    active -= 1

    threads = [
        threading.Thread(target=bounded_parse.parse, args=(Watched(), b"package x\n"))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most == 1


def test_the_headers_of_one_source_stop_at_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_headers as ih
    from server.code.app.index.ingest_stats import FileIngestStats
    from server.code.domain.extract.treesitter import parse_cpp

    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    monkeypatch.setattr(ih, "_needs_header", lambda pf, hd: True)
    read: list[str] = []

    async def header_text(repo: Any, org: str, ns: str, path: str) -> tuple[str, bool]:
        read.append(path)
        return "int f();\n", True

    def costly(hd: Any, path: str, text: str) -> tuple[float, bool]:
        return 0.15, False  # each header takes most of a deadline

    async def none(*a: Any) -> list[str]:
        return []

    class Repo:
        async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
            return set(paths)

    monkeypatch.setattr(ih, "_header_text", header_text)
    monkeypatch.setattr(ih, "_add_header", costly)
    monkeypatch.setattr(ih, "_present_includes", none)
    pf = parse_cpp("src/a.cpp", b"void f() { g(); }\n")
    asyncio.run(
        ih.header_context(
            Repo(), "acme", "code", pf, "src/a.cpp", FileIngestStats(path="src/a.cpp")
        )
    )
    assert len(read) == 2  # 0.15, then 0.30 > 0.2: no third


def test_an_imported_module_whose_parse_stops_reads_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.extract import lang_python

    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.05)
    lang_python._reexport_facts.cache_clear()
    slow = "def g():\n    pass\n" + "x = 1\n" * 400_000  # names `g` when parsed whole
    try:
        assert lang_python._reexport_facts("pkg/__init__.py", slow) == (frozenset(), {}, ())
    finally:
        lang_python._reexport_facts.cache_clear()


def test_helper_parses_that_stop_take_their_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.domain.declaration import Declarations
    from server.code.domain.extract import bounded_parse
    from server.code.domain.extract.lang_detect import detect_shell

    shell, go = b"a=(1 2)\necho hi\n", b"package x\nfunc f() {}\n"
    assert detect_shell("tests/queries/tool", shell) == "shell"
    assert Declarations.parse("x.go", go) is not None

    def stops(parser: Any, source: bytes) -> Any:
        raise bounded_parse.ParseTimeout("stopped")

    monkeypatch.setattr(bounded_parse, "parse", stops)
    assert detect_shell("tests/queries/tool", shell) == ""
    assert Declarations.parse("x.go", go) is None


def test_the_event_loop_runs_while_an_ingest_round_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index.round_reads import RoundReads
    from tests.unit.test_code_ingest_pass_budget import _ReadRepo

    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.5)
    reads = RoundReads(_ReadRepo(), "o", "code-ns")
    ticks = 0

    async def run() -> None:
        nonlocal ticks
        prefetch = asyncio.ensure_future(reads.prefetch({"slow.go": _SLOW[0][1]}))
        while not prefetch.done():
            await asyncio.sleep(0.01)
            ticks += 1
        await prefetch

    asyncio.run(run())
    assert ticks >= 3  # a parse on the event loop would let it tick once


def test_the_round_does_not_parse_a_timed_out_host_shell_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_chunks, round_models
    from server.code.app.index.round_reads import RoundReads
    from tests.unit.test_code_ingest_pass_budget import _ReadRepo

    monkeypatch.setattr(settings, "code_host_islands", True)
    monkeypatch.setattr(settings, "code_parse_timeout_seconds", 0.2)
    sources = {"Makefile": b"build:\n\t" + b"a=(" * 30_000 + b"\n"}
    reads = RoundReads(_ReadRepo(), "o", "code-ns")
    asyncio.run(reads.prefetch(sources))
    assert "ParseTimeout" in reads.parsed["Makefile"][1]

    again: list[str] = []
    monkeypatch.setattr(ingest_chunks, "host_units_of", lambda path, source: again.append(path))
    asyncio.run(round_models._plans(reads, "o", "code-ns", sources, "m", 4000))
    assert again == []  # the chunk plans did not parse the shell again


def test_callee_targets_run_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from server.code.app.index import ingest_edges
    from server.code.domain.extract import edges_treesitter
    from server.code.domain.extract.treesitter import ParsedFile

    threads: list[bool] = []

    def targets(*a: Any) -> dict[str, str]:
        threads.append(threading.current_thread() is threading.main_thread())
        return {"f": "m::f"}

    class Repo:
        async def return_types_for_qualified(self, org: str, ns: str, names: list[str]) -> dict:
            return {"m::f": "m::T"}

    monkeypatch.setattr(edges_treesitter, "callee_targets", targets)
    pf = ParsedFile(path="a.py", lang="python", source=b"")
    asyncio.run(ingest_edges._ts_callee_returns(Repo(), "o", "ns", pf, {}, None))
    assert threads == [False, False]  # both rounds, and neither on the loop's thread
