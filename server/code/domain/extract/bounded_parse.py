"""Tree-sitter parses under a deadline.

Some grammars take quadratic time on short repeated tokens (a run of backticks in Go,
of `<` in JavaScript, of `a=(` in shell), so a 1 MB file can hold a worker or a
serving replica for hours. Every parse of repository text goes through `parse`, which
stops at `code_parse_timeout_seconds` and raises `ParseTimeout`; the callers' parse
failure paths index the file as chunks without symbols.

The parser's own progress callback cannot be used: the binding builds its arguments
with a format code Python 3.12 rejects and crashes. The deadline is instead checked
each time the parser reads the next `_CHUNK` bytes of the source; past it the read
answers end of input, so the parse finishes on what it has read.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_CHUNK = 4096

# The read callback runs Python, where another thread can take the GIL; a tree-sitter
# parser is not safe to enter twice, and parsers are shared (cached per grammar). One
# parse at a time per process is what a parse of bytes, which holds the GIL, gave.
_LOCK = threading.Lock()


class ParseTimeout(Exception):
    """The parse ran past `code_parse_timeout_seconds`."""


def parse(parser: Any, source: bytes) -> Any:
    """`parser.parse(source)`, raising `ParseTimeout` past the deadline."""
    from server.settings import settings

    budget = float(settings.code_parse_timeout_seconds)
    expired = False

    def read(offset: int, _point: Any) -> bytes:
        nonlocal expired
        if expired or time.monotonic() > deadline:
            expired = True
            return b""
        return source[offset : offset + _CHUNK]

    with _LOCK:
        deadline = time.monotonic() + budget
        tree = parser.parse(read)
    if expired:
        raise ParseTimeout(f"parse stopped after {budget:g}s")
    return tree
