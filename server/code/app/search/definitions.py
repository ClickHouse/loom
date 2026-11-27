"""The definition leg: the definitions of the names a query spells out as code,
read from the symbol table and fused as one more reciprocal-rank list. The
lexical leg scores a chunk that defines a name like any chunk that calls it."""

from __future__ import annotations

import logging
import re
from typing import Any

from server.code.app.spans import code_span, span_attrs
from server.code.domain.idents import SpelledName, spelled_names
from server.code.domain.search_scope import SearchScope
from server.code.store.index import CodeRepo, is_noise_path

logger = logging.getLogger(__name__)

# Names read from one query, and name-table keys read per name.
_MAX_NAMES = 32
_SYMBOLS_PER_NAME = 32
# A name defined in more places than this does not say where: `get`, `save`.
_MAX_DEFINITIONS_PER_NAME = 3
# Definitions the leg adds. Reciprocal rank is nearly flat this high, so each scores
# like a top hit of a content leg and takes a place from the legs' own answers. A query
# names its subject first: past the first few, a name is context (a reproducer, a
# traceback) and its definition is rarely the code asked about.
_MAX_DEFINITIONS = 3
# Symbol rows tried for them: a field or a constant has a row and no chunk of its own.
_MAX_CANDIDATES = 4 * _MAX_DEFINITIONS


def named_definitions(
    names: list[SpelledName], symbols: list[dict[str, Any]], *, path_prefix: str = ""
) -> list[tuple[str, str]]:
    """The `(path, qualified name)` definitions to read for `names`, from their symbol
    rows, in the order the query mentions the names. A name keeps its definitions
    outside test paths, or its test ones when it has no other, and is dropped when more
    than `_MAX_DEFINITIONS_PER_NAME` remain."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in symbols:
        by_name.setdefault(str(row["name"]), []).append(row)
    wanted: dict[tuple[str, str], None] = {}
    for name in names:
        rows = by_name.get(name.parts[-1], [])
        if len(rows) >= _SYMBOLS_PER_NAME:
            continue  # the read stopped at its bound: more definitions than it returned
        named = {(str(r["path"]), str(r["qualified_name"])) for r in _matching(name, rows)}
        found = sorted(d for d in named if d[0].startswith(path_prefix))
        found = [d for d in found if not is_noise_path(d[0])] or found
        if len(found) <= _MAX_DEFINITIONS_PER_NAME:
            wanted.update(dict.fromkeys(found))
    return list(wanted)[:_MAX_CANDIDATES]


def _matching(name: SpelledName, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows `name` names. A qualified spelling names the rows whose qualified name
    ends with its parts (`Query.combine` -> `pkg.query::Query::combine`); when none
    does and its last part is a name by itself (`self.save()`), every row."""
    if len(name.parts) == 1:
        return rows
    exact = [
        row
        for row in rows
        if tuple(re.split(r"::|\.", str(row["qualified_name"]))[-len(name.parts) :]) == name.parts
    ]
    return exact if exact or not name.member_alone else rows


async def _definition_leg(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    query: str,
    run: bool,
    work: dict[str, int],
    path_prefix: str,
    scope: SearchScope | None,
) -> list[dict[str, Any]]:
    """The definition leg: the chunks that define the first names `query` spells out.
    Two keyed reads, no embedding; `scope` is the constraint's predicate, None when
    unconstrained."""
    names = spelled_names(query, _MAX_NAMES) if run else []
    if not names:
        return []
    rows: list[dict[str, Any]] = []
    with code_span("search.definition_leg") as sp:
        try:
            symbols = await repo.symbols_named(
                org, ns, sorted({name.parts[-1] for name in names}), per_name=_SYMBOLS_PER_NAME
            )
            wanted = named_definitions(names, symbols, path_prefix=path_prefix)
            order = {definition: rank for rank, definition in enumerate(wanted)}
            found = await repo.definition_chunks(org, ns, model, wanted, scope=scope)
            rows = sorted(
                (r for r in found if (str(r["path"]), str(r["scope"])) in order),
                key=lambda r: order[str(r["path"]), str(r["scope"])],
            )[:_MAX_DEFINITIONS]
        except Exception as e:  # noqa: BLE001 — the definitions are a vote, never a gate
            logger.warning("code.search definition leg failed: %s", e)
        span_attrs(sp, results=len(rows))
    if rows:
        work["named_definitions"] = len(rows)
    return rows
