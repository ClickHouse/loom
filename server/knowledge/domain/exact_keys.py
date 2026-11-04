"""The exact keys a knowledge query may name: identifier-shaped terms (`orders_daily`,
`server.port`, `pool.max-size`, `userId`) the exact-key leg of `knowledge.search`
looks up in the `keys` an extractor gave each chunk (the objects and columns a SQL
statement defines or reads, a TOML key path). Pure.

A term counts when it cannot be a plain word: it has an underscore, a dot, a `$` or
a digit, or a capital after a lowercase letter. A plain word (`status`, `name`) or an
acronym (`ID`, `TTL`) is a column of many tables as often as it is the word a prose
question means, and the lexical leg matches it already; it counts only when it is
the whole query, a lookup of one name. A dash joins the segments of a dotted path (a
TOML bare key may hold one) and nothing else, so prose like `well-known` stays out.

Keys are stored as their source spells them. Each term is looked up as written and
lowercased, so `Events` also finds a key `events`; `events` does not find `Events`.
"""

from __future__ import annotations

import re

# A dotted path whose segments may hold dashes (a qualified table, a TOML key path),
# or a single identifier.
_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*(?:\.[A-Za-z0-9_$-]+)+|[A-Za-z_][A-Za-z0-9_$]*")
# What a plain word or an acronym has none of.
_IDENTIFIER_MARK = re.compile(r"[_.$0-9]|[a-z][A-Z]")
# Terms shorter than this name too many keys to narrow anything.
MIN_KEY_CHARS = 2  # not a setting: a property of what a key is
# The most candidates one query sends to the leg.
MAX_QUERY_KEYS = 32  # not a setting: bounds the statement's parameter


def query_keys(query: str) -> list[str]:
    """The query's candidate keys, in the order written, each once."""
    terms = [t.rstrip("-") for t in _TERM.findall(query or "")]
    terms = [t for t in terms if len(t) >= MIN_KEY_CHARS]
    whole = len(terms) == 1 and (query or "").strip() == terms[0]
    out: dict[str, None] = {}
    for term in terms:
        if whole or _IDENTIFIER_MARK.search(term):
            out.setdefault(term, None)
            out.setdefault(term.lower(), None)
    return list(out)[:MAX_QUERY_KEYS]
