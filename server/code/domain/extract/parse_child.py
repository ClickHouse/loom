"""Tree-sitter parses in a process of their own, under limits: `python -I parse_child.py`,
started and fed by `bounded_parse`. Each request on stdin is a header (`HEADER`: the
grammar's name length, the source length, CPU seconds, address-space bytes), the grammar
(`module:function`) and the source; the address space is what the parse may take past
the process's own when it starts. The process parses it, reading the source in chunks
until the CPU seconds are spent, and answers one byte: `DONE` (read in full and finished
within the CPU seconds) or `STOPPED` (past them), uppercase when the process then exits
because it grew. A parse past the address-space limit, or well past the CPU seconds
after its last read (the kernel's RLIMIT_CPU, whole seconds), ends the process: no
answer. Imports nothing of the server.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import math
import os
import resource
import struct
import sys
import time
from pathlib import Path
from typing import Any

HEADER = struct.Struct("<IIdQ")
DONE, STOPPED = b"d", b"s"
CHUNK = 256
_PR_SET_DUMPABLE = 4
_RETIRE_GROWTH = 64 << 20  # past the interpreter and every grammar's mapping


def language(grammar: str) -> Any:
    """The tree-sitter `Language` that `grammar` (`module:function`) names."""
    import tree_sitter

    module, _, function = grammar.partition(":")
    return tree_sitter.Language(getattr(importlib.import_module(module), function)())


def _parse(parser: Any, source: bytes, budget: float) -> bytes:
    deadline = time.thread_time() + budget
    stopped = False

    def read(offset: int, _point: Any) -> bytes:
        nonlocal stopped
        if offset >= len(source):
            return b""
        if stopped or time.thread_time() > deadline:
            stopped = True
            return b""
        return source[offset : offset + CHUNK]

    parser.parse(read)
    # work after the last read counts too, so the server's own parse stays in the deadline
    return STOPPED if stopped or time.thread_time() > deadline else DONE


def _within(soft: int, inherited: tuple[int, int]) -> int:
    """`soft`, lowered to the inherited soft limit."""
    return soft if inherited[0] == resource.RLIM_INFINITY else min(soft, inherited[0])


def _address_space() -> int:
    """This process's address space in bytes now (0 off Linux)."""
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[0])
    except OSError:
        return 0
    return pages * os.sysconf("SC_PAGE_SIZE")


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # the first process the kernel ends when memory runs out (before the server); written
    # before the process is made non-dumpable, which hands /proc/self to root
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")
    # not dumpable: a parse ended by a signal starts no core dump handler either
    if sys.platform == "linux":
        ctypes.CDLL(None).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    inherited_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    inherited_as = resource.getrlimit(resource.RLIMIT_AS)
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    start = _address_space()
    parsers: dict[str, Any] = {}
    while header := stdin.read(HEADER.size):
        grammar_len, source_len, budget, memory = HEADER.unpack(header)
        grammar = stdin.read(grammar_len).decode()
        source = stdin.read(source_len)
        if grammar not in parsers:
            import tree_sitter

            parsers[grammar] = tree_sitter.Parser(language(grammar))
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu = math.ceil(usage.ru_utime + usage.ru_stime + budget) + 1
        cpu = _within(cpu, inherited_cpu)
        space = _within(_address_space() + memory, inherited_as)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, inherited_cpu[1]))
        resource.setrlimit(resource.RLIMIT_AS, (space, inherited_as[1]))
        try:
            answer = _parse(parsers[grammar], source, budget)
        except Exception:  # noqa: BLE001 — any error mid-parse ends it, as a limit hit does
            os.abort()
        # the next request is read under the limits this process started with
        resource.setrlimit(resource.RLIMIT_CPU, inherited_cpu)
        resource.setrlimit(resource.RLIMIT_AS, inherited_as)
        # Memory a parse freed stays mapped and would raise the next parse's limit: a
        # process grown more than `_RETIRE_GROWTH` past its start (its grammars included)
        # retires.
        retire = _address_space() > start + _RETIRE_GROWTH
        stdout.write(answer.upper() if retire else answer)
        stdout.flush()
        if retire:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
