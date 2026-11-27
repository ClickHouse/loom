"""Code-shaped identifiers named inline in a natural-language query. Pure.

Identifiers must be code-shaped (camelCase / PascalCase with 2+ humps, snake_case, or
ns-qualified) so plain English words never match. code.search's embedded-symbol prior and
memory's code grounding read the same detector. `spelled_names` also reads the names a
query marks as code by calling, backticking or qualifying them, for code.search's
definition leg."""

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


_NAME = r"[A-Za-z_]\w*"
# A name with the owners it is written with: `combine`, `Query.combine`, `DB::Field`,
# `parser->actions`.
_SPELLED_RE = re.compile(rf"(?<!\w){_NAME}(?:(?:\.|::|->){_NAME})*")
_ACCESS_RE = re.compile(r"\.|::|->")
_PLURAL_RE = re.compile(r"\(e?s\)")  # "model(s)" is a plural, not a call
# A capital inside the word: `executeStep`, `QuerySet`, `S3Queue`, `SQLCompiler`; not
# `Field`, `HTTP` or the plural `URLs`.
_HUMP_RE = re.compile(r"[a-z0-9][A-Z]|[A-Z]{2}[a-z]{2}")
# The object or class the code is written inside. It names no owner and no definition.
_RECEIVERS = frozenset({"self", "cls", "this", "super", "Self"})


def spelled_names(query: str, limit: int) -> list[tuple[str, ...]]:
    """The first `limit` names a query spells out as code, each as its parts
    (`Query.combine` -> `("Query", "combine")`), in the order it mentions them.

    A name counts when it is called (`timesince()`), backticked, qualified
    (`Query.combine`, `DB::Field`) or code-shaped (an underscore or a capital inside:
    `bulk_update`, `executeStep`). A plain word is never one, so an ordinary word that
    is also a symbol name is not looked up. A path component (`models/query.py`) is not
    a name either. A receiver is dropped (`self.save_base()` spells `save_base`) and is
    no name by itself (`super()`); any other owner stays as written, so `json.dumps()`
    does not spell `dumps`. A member of something that is not a name
    (`get_parser()._actions`, `rows[0]->name`) has an owner the text does not spell,
    and is no name."""
    found: dict[tuple[str, ...], None] = {}
    for m in _SPELLED_RE.finditer(query):
        ahead, after = query[max(m.start() - 2, 0) : m.start()], query[m.end() : m.end() + 4]
        before = ahead[-1:]
        if before == "/" or after.startswith("/") or ahead.endswith((".", "::", "->")):
            continue
        parts = tuple(_ACCESS_RE.split(m.group(0)))
        lead = 0
        while lead < len(parts) - 1 and parts[lead] in _RECEIVERS:
            lead += 1
        parts = parts[lead:]
        last = parts[-1]
        if len(last.strip("_")) < 2 or last in _RECEIVERS:
            continue
        called = after.startswith("(") and not _PLURAL_RE.match(after)
        ticked = before == "`" and after.startswith("`")
        if len(parts) == 1 and not (called or ticked or "_" in last or _HUMP_RE.search(last)):
            continue
        if parts not in found and len(found) >= limit:
            break
        found[parts] = None
    return list(found)
