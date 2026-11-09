"""Tree-sitter parses under a CPU deadline and a memory limit.

Some grammars take quadratic time on short repeated tokens, and some quadratic memory
(gigabytes from a few tens of kilobytes), so one file could hold a worker or a serving
replica for hours or exhaust its memory. Every parse of repository text goes through
`parse`, which first parses the source in a child process (`parse_child`) held to
`code_parse_memory_mb` of address space past its own and `code_parse_timeout_s` of
CPU time, and raises `ParseTimeout` when that parse runs past the deadline or ends the
child; each caller takes its parse failure path (an indexed file is then chunks
without symbols).

Only a source the child parsed in full within the deadline is parsed again here, where
the same parse does the same work and so stays within the same limits (past twice the
deadline it stops reading, in case it does not). A child serves one parse at a time and
is kept for the next; one that ended is replaced. One parse runs at a time in each
server process, child included, so a server process holds about one memory limit's
worth of parse at once: `code_parse_memory_mb` plus up to about 170 MB, the child's own
40 MB, up to 64 MB it kept from earlier parses, and as much again in the server, which
takes fresh what the child's parse reused. The trees its callers keep are not counted.
The child is the server's interpreter with `-I` and an empty environment, so it loads
the grammars from that interpreter's own site-packages (no `PYTHONPATH`, no user site);
it bounds resources and is no sandbox, a local process started from `domain/` beside
the parsers that call it (no ClickHouse or network). The limits hold on Linux.

The parse here reads the source `_CHUNK` bytes at a time through a callback: the binding
holds the GIL while it parses, and the callback is where another thread can take it, so
a parse run in a thread lets the event loop run every few tens of milliseconds. Work
between reads and after the last one holds the GIL, for at most a tenth of the deadline
at a stretch (the child refuses a parse past that). The binding's own progress callback
is not used: it builds its arguments with a format code Python 3.12 rejects, and crashes.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import IO, Any, NamedTuple, NoReturn, cast

from server.code.domain.extract import parse_child

logger = logging.getLogger("loom.code_index.parse")

_CHUNK = parse_child.CHUNK
_CHILD = Path(parse_child.__file__)

# Every grammar a parse may use, as the child loads it (`module:function`).
_LOADERS = (
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


class LockWait:
    """A caller's limit on waiting for that lock: until `deadline` (monotonic), and whether
    a parse gave up at it (its parser may have read the refusal as a fact of the file)."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds
        self.gave_up = False


# Set by a caller that gives up sooner than a parse may take, so its threads (which copy
# the context) do not parse after it went away. None: wait as long as it takes.
LOCK_WAIT: ContextVar[LockWait | None] = ContextVar("bounded_parse_lock_wait", default=None)

_idle: subprocess.Popen[bytes] | None = None  # the child waiting for its next parse (`_LOCK`)
_SILENT = b"?"  # no answer within the wall bound


class ParseTimeout(Exception):
    """The parse ran past `code_parse_timeout_s` or `code_parse_memory_mb`."""


@functools.cache
def _grammar(language: Any) -> str:
    """The `_LOADERS` entry that loads `language`."""
    for grammar in _LOADERS:
        try:
            loaded = parse_child.language(grammar)
        except Exception:  # noqa: BLE001 — a grammar that does not load matches none
            continue
        if loaded == language:
            return grammar
    raise LookupError(f"no grammar in bounded_parse._LOADERS loads {language!r}")


def _check_in_child(grammar: str, source: bytes, budget: float, memory: int) -> None:
    """Return when the child parsed `source` in full within the limits; raise otherwise."""
    global _idle
    child, _idle = _idle, None
    # Again on a fresh child, once each, when this one ended before reading the request,
    # or the kernel killed it (memory ran out elsewhere: a parse past its own limit ends
    # by another signal).
    unread = killed = False
    while True:
        if child is None:
            child = _start()
        answer, keep = _ask(child, grammar, source, budget, memory)
        if keep:
            _idle = child
        if answer == parse_child.DONE:
            return
        if answer == parse_child.STOPPED:
            raise ParseTimeout(
                f"parse ran past {budget:g} CPU seconds, or {budget / 10:g} s at a stretch"
            )
        if answer == _SILENT:
            raise ParseTimeout(f"the parse process did not answer within {_wall_bound(budget):g} s")
        status = cast("int", child.returncode)
        if answer is None and not unread:
            unread = True
        elif status == -signal.SIGKILL and not killed:
            killed = True
        else:
            _ended(status, budget, memory)
        child = None


def _ended(status: int, budget: float, memory: int) -> NoReturn:
    """The error for a child that ended with `status` instead of answering."""
    if status > 0:
        logger.error("bounded_parse: the parse process failed with status %d", status)
        raise RuntimeError(f"the parse process failed with status {status}")
    if status == -signal.SIGKILL:
        raise ParseTimeout("the parse process was killed twice: memory ran out around it")
    raise ParseTimeout(
        f"parse ran past {memory >> 20} MB or {budget:g} CPU seconds"
        f", or crashed (its process ended by signal {-status})"
    )


def _start() -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(
            [sys.executable, "-I", str(_CHILD)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # a limit hit is its exit status, not a log line
            env={},
            start_new_session=True,  # a terminal's Ctrl-C is the server's to take
        )
    except OSError as e:
        logger.error("bounded_parse: the parse process did not start: %s", e)
        raise


def _wall_bound(budget: float) -> float:
    """Seconds to wait for a child's answer before counting it as ended."""
    return 10 * budget + 5


class _Reply(NamedTuple):
    # `DONE` or `STOPPED`; b"" when the parse ended the child, `_SILENT` when its answer
    # did not come within the wall bound (suspended, or never scheduled), None when the
    # request could not be written to it
    answer: bytes | None
    keep: bool  # it answered and stays for the next parse (an uppercase answer: it retires)


def _ask(
    child: subprocess.Popen[bytes], grammar: str, source: bytes, budget: float, memory: int
) -> _Reply:
    """One child's reply; a child not kept is reaped."""
    stdin, stdout = cast("IO[bytes]", child.stdin), cast("IO[bytes]", child.stdout)
    name = grammar.encode()
    answer: bytes | None = None
    try:
        stdin.write(parse_child.HEADER.pack(len(name), len(source), budget, memory))
        stdin.write(name)
        stdin.write(source)
        stdin.flush()
        answer = _SILENT
        with selectors.DefaultSelector() as waiting:  # not select(): no descriptor cap
            waiting.register(stdout, selectors.EVENT_READ)
            if waiting.select(_wall_bound(budget)):
                answer = stdout.read(1)
    except BrokenPipeError:
        pass  # the child had ended
    finally:
        keep = answer in (parse_child.DONE, parse_child.STOPPED)
        if not keep:
            child.kill()
            _close(child)
    if answer in (parse_child.DONE.upper(), parse_child.STOPPED.upper()):
        answer = answer.lower()
    return _Reply(answer, keep)


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

    limit = LOCK_WAIT.get()
    if limit is None:
        _LOCK.acquire()
    elif not _LOCK.acquire(timeout=max(0.0, limit.deadline - time.monotonic())):
        limit.gave_up = True
        raise ParseTimeout("gave up waiting for another parse in this process")
    try:
        _check_in_child(_grammar(parser.language), source, budget, memory)
        deadline = time.thread_time() + 2 * budget
        try:
            tree = parser.parse(read)
        finally:
            deadline = 0.0
    finally:
        _LOCK.release()
    if expired:
        raise ParseTimeout(f"parse stopped after {2 * budget:g} CPU seconds in the server")
    return tree
