"""The query-shape classifier: symbol, issue or hybrid, deterministic and
LLM-free. The routes and the history leg key on its answer."""

from __future__ import annotations

import re

# Symbol-shaped: one token that looks like an identifier/qualified name or a
# file:line pointer — and nothing that reads like natural language.
# :: (C++/index-canonical) or . (python surface form) qualification both
# read as symbol-shaped: agents ask for python symbols the way python spells
# them (ClickHouseCluster.wait_zookeeper_to_start), and an exact lookup
# should not go through semantic search.
_QUALIFIED_RE = re.compile(r"^~?\w+((?:::|\.)~?\w+)+$")


_FILE_LINE_RE = re.compile(r"^[\w./-]+\.(h|hpp|cpp|cc|cxx|inl|ipp)(:\d+)?$", re.I)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


def classify_query_shape(query: str) -> str:
    """'symbol' | 'issue' | 'hybrid'. Deterministic, unit-tested; false hybrids are fine
    (they still find the symbol), false symbols are not (they'd skip semantic search) —
    so multi-word queries are never symbol. 'issue' is a hybrid query that reads like a
    bug report; every hybrid path treats it as hybrid, the history leg keys on it."""
    q = query.strip()
    if not q or " " in q:
        return "issue" if is_issue_shaped(q) else "hybrid"
    if _QUALIFIED_RE.match(q) or _FILE_LINE_RE.match(q):
        return "symbol"
    if _IDENTIFIER_RE.match(q) and (
        "_" in q or (any(c.isupper() for c in q[1:]) and not q.isupper())
    ):
        return "symbol"
    return "hybrid"


# Issue-shaped: the vocabulary of a bug report (an error or exception was
# raised, a stack was printed, something crashed or failed), or a long
# prose description. Matched on
# lowercased word tokens by prefix so "crashes", "failed", "exceptions"
# count; the family is deliberately small — a false "issue" only adds a
# leg, a false "hybrid" only withholds one.
_ISSUE_MIN_WORDS = 12


_ISSUE_TOKEN_PREFIXES = (
    "error",
    "exception",
    "stack",
    "trace",
    "backtrace",
    "segfault",
    "sigsegv",
    "crash",
    "abort",
    "assert",
    "fail",
    "bug",
    "wrong",
    "incorrect",
    "unexpected",
    "regression",
    "hang",
    "leak",
    "timeout",
    "corrupt",
    "logical_error",
    "cannot",
    "panic",
    "broken",
)


_ISSUE_WORD_RE = re.compile(r"[A-Za-z_]+")


def is_issue_shaped(query: str) -> bool:
    """>= 12 words, or error/exception/stack vocabulary."""
    words = query.split()
    if len(words) >= _ISSUE_MIN_WORDS:
        return True
    for m in _ISSUE_WORD_RE.finditer(query.lower()):
        w = m.group(0)
        if any(w.startswith(p) for p in _ISSUE_TOKEN_PREFIXES):
            return True
    return False
