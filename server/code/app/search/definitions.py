"""The definition leg: the definitions of the first name a query spells out as code,
read from the symbol table and served directly after the first unit of the ranking.
The lexical leg scores a chunk that defines a name like any chunk that calls it."""

from __future__ import annotations

import logging
import re
from typing import Any

from server.code.app.search.priors import _noise_path, asks_for_tests
from server.code.app.spans import code_span, span_attrs
from server.code.domain.idents import spelled_names
from server.code.domain.search_scope import SearchScope
from server.code.domain.units import CodeUnit, unit_from_chunk
from server.code.store.index import CodeRepo, is_noise_path

logger = logging.getLogger(__name__)

# Names read from one query, and name-table keys read per name or qualified spelling.
_MAX_NAMES = 32  # not a setting: a bound on one query's reads, the same on every deployment
_SYMBOLS_PER_NAME = 32
# A name defined in more places than this does not say where: `get`, `save`. Each
# definition served takes one place after the first unit. The value is a judgement: no
# measurement separates it from one or two.
_MAX_DEFINITIONS_PER_NAME = 3  # not a setting: what an ambiguous name is, not a capacity


def named_definitions(
    names: list[tuple[str, ...]],
    symbols: list[dict[str, Any]],
    *,
    path_prefix: str = "",
    asks_tests: bool,
) -> list[list[tuple[str, str]]]:
    """The `(path, qualified name)` definitions of each of `names` that says where it is
    defined, from their symbol rows, in the order the query mentions the names. A name
    keeps its definitions outside test and vendored paths, or those ones when it has no
    other, and is dropped when more than `_MAX_DEFINITIONS_PER_NAME` remain under
    `path_prefix`. A definition on a path the noise penalty demotes is no candidate
    unless the query `asks_tests`: the place after the first unit would undo the
    penalty. The read bound is the namespace's: a name with `_SYMBOLS_PER_NAME` live
    keys there is dropped whatever the prefix."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in symbols:
        by_name.setdefault(str(row["name"]), []).append(row)
    wanted: list[list[tuple[str, str]]] = []
    for name in names:
        rows = _matching(name, by_name.get(name[-1], []))
        if len(rows) >= _SYMBOLS_PER_NAME:
            continue  # the read stopped at its bound: more definitions than it returned
        named = {(str(r["path"]), str(r["qualified_name"])) for r in rows}
        found = sorted(
            d
            for d in named
            if d[0].startswith(path_prefix) and (asks_tests or not _noise_path(d[0]))
        )
        found = [d for d in found if not is_noise_path(d[0])] or found
        if found and len(found) <= _MAX_DEFINITIONS_PER_NAME:
            wanted.append(found)
    return wanted


def _matching(name: tuple[str, ...], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows `name` names: every row for a bare name, and for a qualified spelling
    those whose qualified name ends with its parts (`Query.combine` ->
    `pkg.query::Query::combine`). An owner the symbol table does not record for the
    member names nothing: a module of another library (`json.dumps()`), a variable
    (`qs.filter()`), a class that inherits the member."""
    if len(name) == 1:
        return rows
    return [
        row
        for row in rows
        if tuple(re.split(r"::|\.", str(row["qualified_name"]))[-len(name) :]) == name
    ]


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
    """The definition leg: the chunks that define the first name `query` spells out
    that has one (a field or a constant has a symbol row and no chunk of its own). One
    name, because the order of mention ranks nothing and each definition takes a place
    from the legs' answers. Two keyed reads, no embedding; `scope` is the constraint's
    predicate, None when unconstrained, and filters the chunks after the names are
    counted."""
    names = spelled_names(query, _MAX_NAMES) if run else []
    if not names:
        return []
    rows: list[dict[str, Any]] = []
    with code_span("search.definition_leg") as sp:
        try:
            symbols = await repo.symbols_named(
                org,
                ns,
                sorted({name[-1] for name in names}),
                qualified=sorted({".".join(name) for name in names if len(name) > 1}),
                per_name=_SYMBOLS_PER_NAME,
            )
            wanted = named_definitions(
                names, symbols, path_prefix=path_prefix, asks_tests=asks_for_tests(query)
            )
            found = await repo.definition_chunks(
                org, ns, model, [d for name in wanted for d in name], scope=scope
            )
            chunk = {(str(r["path"]), str(r["scope"])): r for r in found}
            hits = ([chunk[d] for d in name if d in chunk] for name in wanted)
            rows = next((hit for hit in hits if hit), [])
        except Exception as e:  # noqa: BLE001 — the definitions add to an answer, never gate it
            logger.warning("code.search definition leg failed: %s", e)
        span_attrs(sp, results=len(rows))
    if rows:
        work["named_definitions"] = len(rows)
    return rows


def _held_definitions(rows: list[dict[str, Any]], ranked: list[CodeUnit]) -> list[CodeUnit]:
    """The units of the definition rows no other leg found, in the leg's order. They
    stay out of the stages that order the ranking, which read the whole pool, so the
    other units score as they do without the leg."""
    pooled = {u.unit_id for u in ranked}
    held = [unit_from_chunk(row) for row in rows if str(row["chunk_id"]) not in pooled]
    for u in held:
        u.via = "definition"
    return held


def _place_definitions(
    ranked: list[CodeUnit], held: list[CodeUnit], rows: list[dict[str, Any]]
) -> list[CodeUnit]:
    """Serve the named definitions directly after the first unit of `ranked`, at its
    score: those the ranking holds in its order, then `held`. A definition says what
    the query names, not what it asks about, so it is level with the best answer and
    never above it; one that is already first stays first. Run last in the ranking:
    the priors, the outcome boost, the reranker and the personal tie-break each order
    units by a score gap, and would move a definition placed before them past the
    first unit. So they order the ranking as they do without the leg, and a held
    definition is not read by them."""
    named = {str(row["chunk_id"]) for row in rows}
    if not named or not ranked:
        return ranked
    top, rest = ranked[0], ranked[1:]
    level = [u for u in rest if u.unit_id in named] + held
    return [
        top,
        *(u.model_copy(update={"score": top.score}) for u in level),
        *(u for u in rest if u.unit_id not in named),
    ]
