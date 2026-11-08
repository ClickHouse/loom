"""Tree-sitter parses under a deadline.

Some grammars take quadratic time on short repeated tokens, so a 1 MB file could hold
a worker or a serving replica for hours. Every parse of repository text goes through
`parse`, which stops reading the source after `code_parse_timeout_seconds` of the
parsing thread's CPU time and raises `ParseTimeout`; the callers' parse failure
paths index the file as chunks without symbols. CPU time rather than wall-clock
time, so a busy host does not time out a file that parses in time on an idle one.

The parser's own progress callback cannot be used: the binding builds its arguments
with a format code Python 3.12 rejects and crashes. The parser instead reads the
source `_CHUNK` bytes at a time through a callback that checks the deadline; past it
the callback answers end of input and the parse finishes on what it has read. Work
the parser does after its last read is not bounded. The binding holds the GIL while
it parses, and the callback is where another thread can take it, so a parse run in a
thread lets the event loop run every few tens of milliseconds.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_CHUNK = 256

# The read callback runs Python, where another thread can take the GIL; a tree-sitter
# parser is not safe to enter twice, and most parsers are shared (cached per grammar).
# One parse per grammar at a time keeps a parser to one thread.
_LOCKS: dict[Any, threading.Lock] = {}


class ParseTimeout(Exception):
    """The parse ran past `code_parse_timeout_seconds`."""


def parse(parser: Any, source: bytes) -> Any:
    """`parser.parse(source)`, raising `ParseTimeout` past the deadline."""
    from server.settings import settings

    budget = settings.code_parse_timeout_seconds
    deadline = time.thread_time() + budget  # waiting on the lock costs no CPU time
    expired = False

    def read(offset: int, _point: Any) -> bytes:
        nonlocal expired
        if expired or time.thread_time() > deadline:
            expired = True
            return b""
        return source[offset : offset + _CHUNK]

    lock = _LOCKS.get(parser.language) or _LOCKS.setdefault(parser.language, threading.Lock())
    with lock:
        tree = parser.parse(read)
    if expired:
        raise ParseTimeout(f"parse stopped after {budget:g}s")
    return tree
