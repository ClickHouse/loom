"""The exact keys a knowledge query may name: identifier-shaped terms (`events`,
`server.port`, `retention.rules.days`) the exact-key leg of `knowledge.search`
looks up in the `keys` an extractor gave each chunk (the objects a SQL
statement defines or reads, a TOML key path). Pure.

A term is kept as written and lowercased: SQL names compare without case in
most dialects, and a TOML path is matched as the file spells it.
"""

from __future__ import annotations

import re

# An identifier, or a dotted path of them (a qualified table, a TOML key path).
_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z0-9_$]+)*")
# Terms shorter than this name too many keys to narrow anything.
MIN_KEY_CHARS = 2  # not a setting: a property of what a key is
# The most candidates one query sends to the leg.
MAX_QUERY_KEYS = 32  # not a setting: bounds the statement's parameter


def query_keys(query: str) -> list[str]:
    """The query's candidate keys, in the order written, each once."""
    out: dict[str, None] = {}
    for term in _TERM.findall(query or ""):
        if len(term) < MIN_KEY_CHARS:
            continue
        out.setdefault(term, None)
        out.setdefault(term.lower(), None)
        if len(out) >= MAX_QUERY_KEYS:
            break
    return list(out)[:MAX_QUERY_KEYS]
