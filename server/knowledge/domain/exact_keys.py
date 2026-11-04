"""The exact keys a knowledge query may name: identifier-shaped terms (`orders_daily`,
`server.port`, `retention.rules.days`, `userId`) the exact-key leg of
`knowledge.search` looks up in the `keys` an extractor gave each chunk (the objects
and columns a SQL statement defines or reads, a TOML key path). Pure.

A term counts when it cannot be a plain word: it has an underscore, a dot, a `$` or
a digit, or a capital after its first letter. A plain word (`status`, `name`) is a
column of many tables as often as it is the word a prose question means, and the
lexical leg matches it already; it counts only when it is the whole query, a lookup
of one name. A term is kept as written and lowercased: SQL names compare without
case in most dialects, and a TOML path is matched as the file spells it.
"""

from __future__ import annotations

import re

# An identifier, or a dotted path of them (a qualified table, a TOML key path).
_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*(?:\.[A-Za-z0-9_$]+)*")
# What a plain word has none of.
_IDENTIFIER_MARK = re.compile(r"[_.$0-9]|.[A-Z]")
# Terms shorter than this name too many keys to narrow anything.
MIN_KEY_CHARS = 2  # not a setting: a property of what a key is
# The most candidates one query sends to the leg.
MAX_QUERY_KEYS = 32  # not a setting: bounds the statement's parameter


def query_keys(query: str) -> list[str]:
    """The query's candidate keys, in the order written, each once."""
    terms = [t for t in _TERM.findall(query or "") if len(t) >= MIN_KEY_CHARS]
    whole = len(terms) == 1 and (query or "").strip() == terms[0]
    out: dict[str, None] = {}
    for term in terms:
        if not whole and not _IDENTIFIER_MARK.search(term):
            continue
        out.setdefault(term, None)
        out.setdefault(term.lower(), None)
        if len(out) >= MAX_QUERY_KEYS:
            break
    return list(out)[:MAX_QUERY_KEYS]
