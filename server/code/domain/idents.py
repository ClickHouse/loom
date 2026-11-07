"""Code-shaped identifiers named inline in a natural-language query. Pure.

Identifiers must be code-shaped (camelCase / PascalCase with 2+ humps, snake_case, or
ns-qualified) so plain English words never match. code.search's embedded-symbol prior and
memory's code grounding read the same detector."""

from __future__ import annotations

import re

EMBEDDED_IDENT_RE = re.compile(
    r"\b(?:"
    r"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)+"  # Foo::bar
    r"|[A-Z][a-z0-9]+[A-Z][a-zA-Z0-9]*"  # PascalCase, 2+ humps
    r"|[a-z][a-z0-9]*[A-Z][a-zA-Z0-9]*"  # camelCase
    r"|[a-z][a-z0-9]*(?:_[a-z0-9]+)+"  # snake_case
    r")\b"
)


def embedded_idents(query: str) -> frozenset[str]:
    """Lowercased identifier components named inline in an NL query.

    Qualified names contribute each component ("Foo::bar" -> foo, bar) so a
    unit scoped either way matches.
    """
    out: set[str] = set()
    for m in EMBEDDED_IDENT_RE.findall(query):
        for comp in m.split("::"):
            comp = comp.lstrip("~")
            if len(comp) > 2:
                out.add(comp.lower())
    return frozenset(out)
