"""Code-shaped identifiers named inline in a natural-language query. Pure.

Identifiers must be code-shaped (camelCase / PascalCase with 2+ humps, snake_case, or
ns-qualified) so plain English words never match. code.search's embedded-symbol prior and
memory's code grounding read the same detector. `spelled_names` also reads the names a
query marks as code by calling, backticking or qualifying them, for code.search's
definition leg."""

from __future__ import annotations

import re
from typing import NamedTuple

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


_NAME = r"[A-Za-z_]\w*"
# A name with the owners it is written with: `combine`, `Query.combine`, `DB::Field`.
_SPELLED_RE = re.compile(rf"(?<!\w){_NAME}(?:(?:\.|::){_NAME})*")
_PLURAL_RE = re.compile(r"\(e?s\)")  # "model(s)" is a plural, not a call
# A capital inside the word: `executeStep`, `QuerySet`, `S3Queue`, `SQLCompiler`; not
# `Field`, `HTTP` or the plural `URLs`.
_HUMP_RE = re.compile(r"[a-z0-9][A-Z]|[A-Z]{2}[a-z]{2}")


class SpelledName(NamedTuple):
    """A name a query writes the way code does."""

    # `Query.combine` -> ("Query", "combine")
    parts: tuple[str, ...]
    # The last part names a definition by itself: it is called, backticked or
    # code-shaped. `pkg.db.models` and `settings.py` name one only as written.
    member_alone: bool


def spelled_names(query: str, limit: int) -> list[SpelledName]:
    """The first `limit` names a query spells out as code, in the order it mentions them.

    A name counts when it is called (`timesince()`), backticked, qualified
    (`Query.combine`, `DB::Field`) or code-shaped (an underscore or a capital inside:
    `bulk_update`, `executeStep`). A plain word is never one, so an ordinary word that
    is also a symbol name is not looked up. A path component (`models/query.py`) is not
    a name either."""
    found: dict[tuple[str, ...], bool] = {}
    for m in _SPELLED_RE.finditer(query):
        before, after = query[m.start() - 1 : m.start()], query[m.end() : m.end() + 4]
        if before == "/" or after.startswith("/"):
            continue
        parts = tuple(part for part in re.split(r"::|\.", m.group(0)) if part)
        last = parts[-1]
        if len(last.strip("_")) < 2:
            continue
        called = after.startswith("(") and not _PLURAL_RE.match(after)
        ticked = before == "`" and after.startswith("`")
        alone = called or ticked or "_" in last or bool(_HUMP_RE.search(last))
        if len(parts) == 1 and not alone:
            continue
        if parts not in found and len(found) >= limit:
            break
        found[parts] = found.get(parts, False) or alone
    return [SpelledName(parts, alone) for parts, alone in found.items()]
