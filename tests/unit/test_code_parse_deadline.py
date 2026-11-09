"""Every tree-sitter parse of repository text stops at `code_parse_timeout_s` and
`code_parse_memory_mb`."""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
import subprocess
import sys
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


def _end(child: Any) -> None:
    from server.code.domain.extract import bounded_parse

    if child is not None:
        child.kill()
        bounded_parse._close(child)


@pytest.mark.parametrize(("path", "source"), _SLOW, ids=[p for p, _ in _SLOW])
def test_a_slow_parse_stops_at_the_deadline_and_the_file_is_chunked(
    monkeypatch: pytest.MonkeyPatch, path: str, source: bytes
) -> None:
    from server.code.domain.extract import bounded_parse

    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.2)
    monkeypatch.setattr(bounded_parse, "_idle", None)
    start = time.monotonic()
    pf, err = parse_for_ingest(path, source)
    assert time.monotonic() - start < 5
    assert pf is not None and pf.symbols == [] and pf.source == source
    assert "ParseTimeout" in err
    child = bounded_parse._idle  # kept: it answered
    assert child is not None
    pf, err = parse_for_ingest("ok.go", b"package x\nfunc f() {}\n")
    assert err == "" and pf is not None and len(pf.symbols) == 1
    assert bounded_parse._idle is child
    _end(bounded_parse._idle)


# Parses, then sets every thread's CPU clock past the deadline and reads a node's text
# from another thread, as the event loop reads a tree a worker thread parsed.
_READ_AFTER_DEADLINE = """
import faulthandler, threading, time
faulthandler.dump_traceback_later(30, exit=True)  # a read that spins: dump where, and fail
import tree_sitter_python
from tree_sitter import Language, Parser
from server.code.domain.extract.bounded_parse import parse
tree = parse(Parser(Language(tree_sitter_python.language())), b"x = 'a'.format(1)\\n")
time.thread_time = lambda: float("inf")
texts = []
reader = threading.Thread(target=lambda: texts.append(tree.root_node.children[0].text))
reader.start()
reader.join()
print(texts[0].decode())
"""


def test_a_node_reads_its_text_after_the_deadline_has_passed() -> None:
    # In a child process: a read that spins is killed with it, not left running here.
    done = subprocess.run(
        [sys.executable, "-c", _READ_AFTER_DEADLINE],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=_SERVER.parent,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "x = 'a'.format(1)"


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
    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.2)
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
    """Outside `bounded_parse` and its child, no `.parse(` is called on a parser; and in
    the code plane's domain, or in any module that names tree-sitter, no `.parse(` at all
    except on a receiver in `_NOT_A_PARSER`. A parser is known by its name (`parser`,
    `_parser(...)`), so one held under another name outside those modules is not caught."""
    unbounded = []
    for f in _SERVER.rglob("*.py"):
        if f.name in {"bounded_parse.py", "parse_child.py"}:
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

    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.2)
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


def test_two_threads_never_parse_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    import tree_sitter
    import tree_sitter_go
    import tree_sitter_python

    from server.code.domain.extract import bounded_parse

    monkeypatch.setattr(bounded_parse, "_idle", None)
    active, most = 0, 0
    count = threading.Lock()

    class Watched:
        """A parser that records how many threads are inside any of them."""

        def __init__(self, grammar: Any) -> None:
            self.language = tree_sitter.Language(grammar.language())
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

    real = bounded_parse._ask

    def ask(*args: Any) -> Any:  # the child's part of a parse counts too
        nonlocal active, most
        with count:
            active += 1
            most = max(most, active)
        try:
            return real(*args)
        finally:
            with count:
                active -= 1

    monkeypatch.setattr(bounded_parse, "_ask", ask)
    threads = [
        threading.Thread(target=bounded_parse.parse, args=(Watched(grammar), b"x\n"))
        for grammar in (tree_sitter_go, tree_sitter_python) * 2
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most == 1
    _end(bounded_parse._idle)


def test_headers_whose_parses_stop_spend_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import ingest_headers as ih
    from server.code.app.index.ingest_stats import FileIngestStats
    from server.code.domain.extract.treesitter import parse_cpp

    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.2)
    monkeypatch.setattr(ih, "_needs_header", lambda pf, hd: True)
    monkeypatch.setattr(ih, "_header_cache", {})
    read: list[str] = []

    async def header_text(repo: Any, org: str, ns: str, path: str) -> tuple[str, bool]:
        read.append(path)
        return "'\\''" * 30_000, True

    async def none(*a: Any) -> list[str]:
        return []

    class Repo:
        async def live_paths_present(self, org: str, ns: str, paths: list[str]) -> set[str]:
            return set(paths)

    monkeypatch.setattr(ih, "_header_text", header_text)
    monkeypatch.setattr(ih, "_present_includes", none)
    pf = parse_cpp("src/a.cpp", b"void f() { g(); }\n")
    stats = FileIngestStats(path="src/a.cpp")
    asyncio.run(ih.header_context(Repo(), "acme", "code", pf, "src/a.cpp", stats))
    assert len(read) == 1  # it spent the deadline: no second


def test_the_headers_of_one_source_stop_at_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_headers as ih
    from server.code.app.index.ingest_stats import FileIngestStats
    from server.code.domain.extract.treesitter import parse_cpp

    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.2)
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

    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.05)
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
    from server.code.domain.extract import bounded_parse, parse_child
    from tests.unit.test_code_ingest_pass_budget import _ReadRepo

    # the child passes it, so the server makes the whole slow parse itself
    monkeypatch.setattr(bounded_parse, "_in_child", lambda *a: parse_child.DONE)
    source = b"package x\nvar s = " + b"`" * 12_000
    reads = RoundReads(_ReadRepo(), "o", "code-ns")
    ticks = 0

    async def run() -> None:
        nonlocal ticks
        prefetch = asyncio.ensure_future(reads.prefetch({"slow.go": source}))
        while not prefetch.done():
            await asyncio.sleep(0.01)
            ticks += 1
        await prefetch

    asyncio.run(run())
    assert ticks >= 3  # a parse on the event loop would let it tick once
    assert reads.parsed["slow.go"][1] == ""


def test_the_round_does_not_parse_a_timed_out_host_shell_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest_chunks, round_models
    from server.code.app.index.round_reads import RoundReads
    from tests.unit.test_code_ingest_pass_budget import _ReadRepo

    monkeypatch.setattr(settings, "code_host_islands", True)
    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.2)
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


# Inputs some grammars parse in quadratic memory: each needs gigabytes unbounded
_COSTLY = [
    ("costly.java", b"a=>" * 4_000),
    ("costly.cpp", b"a<-" * 12_000),
    ("costly.ts", b"a<b," * 12_000),
]


@pytest.mark.parametrize(("path", "source"), _COSTLY, ids=[p for p, _ in _COSTLY])
def test_a_parse_past_the_memory_limit_is_refused_and_the_server_stays_small(
    path: str, source: bytes
) -> None:
    """Run in a process held to 1 GB of address space, so an unbounded parse fails the
    test (a crash) instead of taking the host's memory."""
    script = f"""
import resource
resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
from server.code.app.index.ingest_parse import parse_for_ingest
from server.settings import settings
settings.code_parse_memory_mb = 256
settings.code_parse_timeout_s = 30.0  # only the memory limit stops it
pf, err = parse_for_ingest({path!r}, {source!r})
assert pf is not None and pf.symbols == [] and "ParseTimeout" in err, err
pf, err = parse_for_ingest("Ok.java", b"class Ok {{ void f() {{}} }}")  # a new child
assert err == "" and pf is not None and pf.symbols, err
print("refused")
"""
    done = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_SERVER.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0 and "refused" in done.stdout, done.stderr[-2000:]
    assert "ended with status -" in done.stderr  # logged: the limit ended its process


def test_every_grammar_the_server_imports_is_one_the_child_loads() -> None:
    from server.code.domain.declaration import _GRAMMARS
    from server.code.domain.extract import bounded_parse

    listed = {g.partition(":")[0] for g in bounded_parse._GRAMMARS}
    imported = {f"tree_sitter_{g}" for g in _GRAMMARS.values()}
    for f in _SERVER.rglob("*.py"):
        imported |= set(re.findall(r"(?:from|import) (tree_sitter_\w+)", f.read_text()))
    assert imported and imported <= listed
    from server.code.domain.declaration import _parser
    from server.code.domain.extract.lang_detect import _bash_parser

    for parser in [_parser(g) for g in (*_GRAMMARS, "tsx")] + [_bash_parser()]:
        bounded_parse._grammar(parser.language)  # each Language the server builds matches


def test_a_child_that_ended_while_idle_is_replaced_without_failing_the_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.extract import bounded_parse

    monkeypatch.setattr(bounded_parse, "_idle", None)
    source = b"package x\nfunc f() {}\n"
    assert parse_for_ingest("a.go", source)[1] == ""
    child = bounded_parse._idle
    assert child is not None
    child.kill()  # ends while idle; it is still the idle child
    child.wait()
    pf, err = parse_for_ingest("b.go", source)
    assert err == "" and pf is not None and len(pf.symbols) == 1
    _end(bounded_parse._idle)


def test_a_child_reads_its_next_request_under_no_earlier_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.domain.extract import bounded_parse, parse_child

    monkeypatch.setattr(bounded_parse, "_idle", None)
    grammar = "tree_sitter_go:language"
    assert bounded_parse._in_child(grammar, b"package x\n", 3.0, 256 << 20) == parse_child.DONE
    child = bounded_parse._idle
    assert child is not None
    # a limit the next request's 8 MB source would not fit under
    assert bounded_parse._in_child(grammar, b"package x\n", 3.0, 4 << 20) == parse_child.DONE
    big = b"package x\n// " + b"x" * (8 << 20)
    assert bounded_parse._in_child(grammar, big, 3.0, 256 << 20) == parse_child.DONE
    assert bounded_parse._idle is child
    _end(bounded_parse._idle)


def test_a_parse_in_the_server_past_twice_the_deadline_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tree_sitter
    import tree_sitter_go

    from server.code.domain.extract import bounded_parse, parse_child

    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.05)
    monkeypatch.setattr(bounded_parse, "_in_child", lambda *a: parse_child.DONE)
    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_go.language()))
    with pytest.raises(bounded_parse.ParseTimeout, match="in the server"):
        bounded_parse.parse(parser, _SLOW[0][1])


def test_a_child_on_a_descriptor_past_1024_answers() -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 1200:
        pytest.skip("this host allows fewer than 1200 open files")
    script = """
import os, resource
resource.setrlimit(resource.RLIMIT_NOFILE, (1200, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
held = [os.dup(0) for _ in range(1100)]
from server.code.app.index.ingest_parse import parse_for_ingest
pf, err = parse_for_ingest("a.go", b"package x\\nfunc f() {}\\n")
assert err == "" and pf is not None and len(pf.symbols) == 1, err
"""
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=_SERVER.parent, capture_output=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_a_child_that_does_not_answer_is_ended(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    from server.code.domain.extract import bounded_parse

    monkeypatch.setattr(bounded_parse, "_idle", None)
    source = b"package x\nfunc f() {}\n"
    assert parse_for_ingest("a.go", source)[1] == ""
    child = bounded_parse._idle
    assert child is not None
    child.send_signal(signal.SIGSTOP)
    monkeypatch.setattr(settings, "code_parse_timeout_s", 0.05)
    start = time.monotonic()
    pf, err = parse_for_ingest("b.go", source)
    assert "ParseTimeout" in err and time.monotonic() - start < 15
    pf, err = parse_for_ingest("c.go", source)  # a fresh child
    assert err == "" and pf is not None and len(pf.symbols) == 1
    _end(bounded_parse._idle)


def test_the_memory_setting_decides_the_refusal() -> None:
    """An input of about 200 MB: refused at 128 MB, parsed at 512 MB. In a process held
    to 1 GB, which the child inherits but does not reach either way."""
    script = """
import resource
resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
from server.code.app.index.ingest_parse import parse_for_ingest
from server.settings import settings
settings.code_parse_timeout_s = 30.0
source = b"a<-" * 2_400
settings.code_parse_memory_mb = 128
assert "ParseTimeout" in parse_for_ingest("a.cpp", source)[1]
settings.code_parse_memory_mb = 512
assert parse_for_ingest("a.cpp", source)[1] == ""
from server.code.domain.extract import bounded_parse
assert bounded_parse._idle is None  # it grew past 64 MB and retired
settings.code_parse_memory_mb = 2048  # past the inherited 1 GB: lowered to it
assert parse_for_ingest("b.cpp", b"int f();\\n")[1] == ""
"""
    done = subprocess.run(
        [sys.executable, "-c", script], cwd=_SERVER.parent, capture_output=True, timeout=60
    )
    assert done.returncode == 0, done.stderr[-2000:]


def test_a_parse_whose_child_the_kernel_killed_runs_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal
    import threading

    from server.code.domain.extract import bounded_parse

    monkeypatch.setattr(bounded_parse, "_idle", None)
    real = bounded_parse._start
    started: list[Any] = []

    def start() -> Any:
        child = real()
        if not started:  # the first child is killed from outside mid-parse
            threading.Timer(0.1, child.send_signal, (signal.SIGKILL,)).start()
        started.append(child)
        return child

    monkeypatch.setattr(bounded_parse, "_start", start)
    source = b"package x\nvar s = " + b"`" * 16_000  # parses in over a second
    pf, err = parse_for_ingest("a.go", source)
    assert err == "" and pf is not None and len(started) == 2
    _end(bounded_parse._idle)
