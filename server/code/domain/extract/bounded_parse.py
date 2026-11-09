"""Tree-sitter parses under a CPU deadline and a memory limit.

Some grammars take quadratic time on short repeated tokens, and some quadratic memory
(gigabytes from a few tens of kilobytes), so one file could hold a worker or a serving
replica for hours or exhaust its memory. Every parse of repository text goes through
`parse`, which first parses the source in a child process (`parse_child`) held to
`code_parse_memory_mb` of address space past its own and `code_parse_timeout_s` of CPU
time, and raises `ParseTimeout` when that parse runs past the deadline or ends the child; each
caller takes its parse failure path (an indexed file is then chunks without symbols).
Only a source the child parsed in full within the deadline is parsed again here, where
the same parse does the same work and so stays within the same limits (past twice the
deadline it stops reading, in case it does not). A child serves one parse at a time and
is kept for the next; one that ended is replaced. One parse runs at a time in each
server process, child included, so a server process holds at most one memory limit's
worth of parse at once (the trees its callers keep are not counted). The child is the
server's interpreter with `-I` and an empty environment; it bounds resources and is no
sandbox. The pool stays in `domain/`, beside the parsers that call it: the child is a
local process, no ClickHouse or network. The limits hold on Linux.

The parse here reads the source `_CHUNK` bytes at a time through a callback: the binding
holds the GIL while it parses, and the callback is where another thread can take it, so
a parse run in a thread lets the event loop run every few tens of milliseconds. Work
after its last read holds the GIL, within the deadline.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any, cast

from server.code.domain.extract import parse_child

logger = logging.getLogger("loom.code_index.parse")

_CHUNK = parse_child.CHUNK
_CHILD = Path(parse_child.__file__)

# Every grammar a parse may use, as the child loads it (`module:function`).
_GRAMMARS = (
    "tree_sitter_bash:language",
    "tree_sitter_cpp:language",
    "tree_sitter_go:language",
    "tree_sitter_java:language",
    "tree_sitter_javascript:language",
    "tree_sitter_python:language",
    "tree_sitter_rust:language",
    "tree_sitter_tlaplus:language",
    "tree_sitter_typescript:language_typescript",
    "tree_sitter_typescript:language_tsx",
)

# One parse at a time in this process: it bounds the memory parses hold together, and
# keeps a parser to one thread (the read callback runs Python, where another thread can
# take the GIL; a tree-sitter parser is not safe to enter twice, and most are shared).
_LOCK = threading.Lock()

_idle: subprocess.Popen[bytes] | None = None  # the child waiting for its next parse (`_LOCK`)
_SILENT = b"?"  # no answer within the wall bound


class ParseTimeout(Exception):
    """The parse ran past `code_parse_timeout_s` or `code_parse_memory_mb`."""


@functools.cache
def _grammar(language: Any) -> str:
    """The `_GRAMMARS` entry that loads `language`."""
    for grammar in _GRAMMARS:
        try:
            loaded = parse_child.language(grammar)
        except Exception:  # noqa: BLE001 — a grammar that does not load matches none
            continue
        if loaded == language:
            return grammar
    raise LookupError(f"no grammar in bounded_parse._GRAMMARS loads {language!r}")


def _in_child(grammar: str, source: bytes, budget: float, memory: int) -> bytes | int:
    """The child's answer for one parse: `parse_child.DONE`, `STOPPED`, or `_SILENT`
    (it was ended for not answering); or, when the parse ended the child, its exit
    status (negative: the signal that ended it)."""
    global _idle
    child, _idle = _idle, None
    answer = None if child is None else _ask(child, grammar, source, budget, memory)
    if answer is None:  # none idle, or it ended while idle, before reading the request
        try:
            child = subprocess.Popen(
                [sys.executable, "-I", str(_CHILD)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env={},
            )
        except OSError as e:
            logger.error("bounded_parse: the parse process did not start: %s", e)
            raise
        answer = _ask(child, grammar, source, budget, memory)
    return answer or cast("int", cast("subprocess.Popen[bytes]", child).returncode)


def _ask(
    child: subprocess.Popen[bytes], grammar: str, source: bytes, budget: float, memory: int
) -> bytes | None:
    """One child's answer, lowercased; b"" when the parse ended it, `_SILENT` when its
    answer did not come within ten deadlines and five seconds of the request (stopped,
    or never scheduled), None when the request could not be written to it. A child that
    ends, goes silent or retires (an uppercase answer) is reaped; any other is kept."""
    global _idle
    stdin, stdout = cast("IO[bytes]", child.stdin), cast("IO[bytes]", child.stdout)
    name = grammar.encode()
    answer: bytes | None = None
    try:
        stdin.write(parse_child.HEADER.pack(len(name), len(source), budget, memory))
        stdin.write(name + source)
        stdin.flush()
        answer = _SILENT
        with selectors.DefaultSelector() as waiting:  # not select(): no descriptor cap
            waiting.register(stdout, selectors.EVENT_READ)
            if waiting.select(10 * budget + 5):
                answer = stdout.read(1)
    except BrokenPipeError:
        pass  # the child had ended
    finally:
        if answer in (parse_child.DONE, parse_child.STOPPED):
            _idle = child
        else:
            child.kill()
            _close(child)
    return answer.lower() if answer else answer


def _close(child: subprocess.Popen[bytes]) -> None:
    child.wait()
    with contextlib.suppress(BrokenPipeError):  # closing flushes what it did not take
        cast("IO[bytes]", child.stdin).close()
    cast("IO[bytes]", child.stdout).close()


def parse(parser: Any, source: bytes) -> Any:
    """`parser.parse(source)`, raising `ParseTimeout` past the deadline or the memory
    limit."""
    from server.settings import settings

    budget = settings.code_parse_timeout_s
    memory = settings.code_parse_memory_mb << 20

    # The same parse finished within the deadline in the child; twice that here means
    # it is not the same parse, and it stops reading. Only while it parses: the tree
    # reads `Node.text` through this callback for as long as it lives.
    deadline = 0.0
    expired = False

    def read(offset: int, _point: Any) -> bytes:
        nonlocal expired
        if offset >= len(source):
            return b""  # the end, read in full
        if deadline and (expired or time.thread_time() > deadline):
            expired = True
            return b""
        return source[offset : offset + _CHUNK]

    with _LOCK:
        answer = _in_child(_grammar(parser.language), source, budget, memory)
        if answer == parse_child.STOPPED:
            raise ParseTimeout(f"parse stopped after {budget:g} CPU seconds")
        if isinstance(answer, int) and answer > 0:
            logger.error("bounded_parse: the parse process failed with status %d", answer)
            raise RuntimeError(f"the parse process failed with status {answer}")
        if answer == _SILENT:
            raise ParseTimeout(f"the parse process did not answer within {10 * budget + 5:g} s")
        if answer != parse_child.DONE:
            raise ParseTimeout(
                f"parse ran past {settings.code_parse_memory_mb} MB or {budget:g} CPU seconds"
                f" (its process ended with status {answer!r})"
            )
        deadline = time.thread_time() + 2 * budget
        try:
            tree = parser.parse(read)
        finally:
            deadline = 0.0
    if expired:
        raise ParseTimeout(f"parse stopped after {2 * budget:g} CPU seconds in the server")
    return tree
