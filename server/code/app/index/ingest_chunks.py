"""The chunk stage of a file's ingest: the chunks a parsed file yields (tree-sitter,
host islands or line-window lanes), the facets that ride them, and the context text
and callee outline each chunk is embedded with."""

from __future__ import annotations

import logging
from typing import Any

from server.code.app.index.ingest_parse import (
    CODE_ISLAND_LANGS,
    LANE_UNIT_LANGS,
    TS_LANGS,
    _code_sql_units,
    _host_islands_on,
    _lane_module,
    _py_islands_on,
    _shell_islands_on,
    _statement_records_on,
)
from server.code.app.index.ingest_resolve import _PY_LAYOUT_CACHE
from server.code.domain.extract.bounded_parse import ParseTimeout
from server.code.domain.extract.chunker import Chunk, chunk_file
from server.code.domain.extract.context_text import build_context_text
from server.code.domain.extract.edges_treesitter import call_sites
from server.code.domain.extract.lang_hosts import host_units
from server.code.domain.extract.lang_py_islands import py_sql_units
from server.code.domain.extract.lang_reference import REFERENCE_LANG, reference_chunk
from server.code.domain.extract.lang_shell_islands import shell_units
from server.code.domain.extract.lang_sql import sql_units
from server.code.domain.extract.lang_sql_model import SQL_LANG, Facet
from server.code.domain.extract.treesitter import ParsedFile
from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.ingest")


def chunk_context_texts(
    ns: str,
    path: str,
    pf: ParsedFile,
    chunks: list[Chunk],
    to_insert: list[Chunk],
    structural_prefixes: dict[int, str],
    llm_descriptions: dict[int, str],
    calls_by_idx: dict[int, Any] | None = None,
    callers_by_idx: dict[int, Any] | None = None,
) -> tuple[dict[int, str], list[str]]:
    """(descriptions, context texts) of the chunks a file ingest inserts — the text each
    one is embedded from. Shared by `ingest_file` and a round's embedding prefetch, so
    both embed the same bytes."""
    descriptions = _structural_descriptions(ns, chunks, to_insert, structural_prefixes)
    descriptions.update(llm_descriptions)  # an LLM one-liner beats the structural prefix
    sig_by_scope = {s.qualified_name: s.signature for s in pf.symbols if s.kind == "function"}
    calls, callers = calls_by_idx or {}, callers_by_idx or {}
    texts = [
        build_context_text(
            path=path,
            scope=c.scope,
            signature=sig_by_scope.get(c.scope, ""),
            description=descriptions.get(i, ""),
            content=c.content,
            calls=calls.get(i, ()),
            callers=callers.get(i, ()),
        )
        for i, c in enumerate(to_insert)
    ]
    return descriptions, texts


def _moved_chunk_spans(
    chunks: list[Chunk], existing_by_id: dict[str, dict[str, Any]], ns: str
) -> dict[str, tuple[int, int, str]]:
    """chunk_id -> (start, end, kind) of the live chunks whose content is unchanged but whose span
    or kind is not. A chunk id is content-addressed, so an edit ABOVE a function leaves its chunk id
    unchanged while every line number below the edit shifts; those rows are re-versioned with the
    new span (embedding and description carried over — nothing to recompute), or code.symbol /
    code.enclosing / grep-shim answers point at the wrong lines for as long as the content stays
    identical. The id sees no KIND either — a chunker that files a wrapped component as `function`
    instead of skeleton windows re-versions the row the same way (kind rides `moved_of`)."""
    out: dict[str, tuple[int, int, str]] = {}
    for c in chunks:
        row = existing_by_id.get(c.chunk_id(ns))
        if row is None:
            continue
        old_span = (int(row.get("start_line") or 0), int(row.get("end_line") or 0))
        old_kind = str(row.get("kind") or c.kind)
        if old_span != (c.start_line, c.end_line) or old_kind != c.kind:
            out[c.chunk_id(ns)] = (c.start_line, c.end_line, c.kind)
    return out


async def _callee_outline(
    repo: CodeRepo,
    org: str,
    callers_ns: str,
    pf: ParsedFile,
    chunks: list[Chunk],
    *,
    mode: str,
    stoplist: frozenset[str],
) -> tuple[dict[int, list[str]], dict[int, list[str]]]:
    """Per new function chunk: its callee names and (mode calls_callers) its caller
    names. Sets `Chunk.extra_lexemes` so the lexical leg sees the caller names the body
    does not carry. Off: two empty maps, and every context text is byte-identical to the
    plain builder's."""
    calls: dict[int, list[str]] = {}
    callers: dict[int, list[str]] = {}
    if mode == "off" or not chunks:
        return calls, callers
    from server.code.domain.extract.callee_outline import (
        callees_for_chunk,
        callers_for_chunk,
        callers_for_chunks,
    )

    fn_chunks = [(i, c) for i, c in enumerate(chunks) if c.kind == "function"]
    if not fn_chunks:
        return calls, callers
    try:
        sites = call_sites(pf)
    except Exception as e:  # noqa: BLE001 — an outline never fails an ingest
        logger.debug("code ingest: call sites failed for %s: %s", pf.path, e)
        sites = []
    by_symbol: dict[str, list[str]] = {}
    if mode == "calls_callers":
        by_symbol = await callers_for_chunks(repo, org, callers_ns, [c for _, c in fn_chunks])
    for i, c in fn_chunks:
        names = callees_for_chunk(sites, c, stoplist=stoplist)
        if names:
            calls[i] = names
        if by_symbol:
            who = callers_for_chunk(c, by_symbol)
            if who:
                callers[i] = who
        extra = [*names, *callers.get(i, [])]
        if extra:
            c.extra_lexemes = " ".join(extra)
    return calls, callers


def _lane_chunks(
    pf: ParsedFile, path: str, source: bytes, max_chunk_chars: int
) -> tuple[list[Chunk], dict[int, str], list[Facet]] | None:
    """The chunks and facets of a TLA+ module, a TLC config, a FlatBuffers schema, a
    CMake file or a Markdown document; None for every other language (the AST
    chunker's)."""
    if pf.lang == "tla" and pf.symbols:
        from server.code.domain.extract.lang_tla import tla_chunks

        return tla_chunks(pf), {}, []
    if pf.lang == "tla_config":
        from server.code.domain.extract.lang_tla import tlc_chunks, tlc_units

        return tlc_chunks(pf), {}, list(tlc_units(path, source).facets)
    if pf.lang == "flatbuffers" and pf.symbols:
        from server.code.domain.extract.lang_flatbuffers import fbs_chunks, fbs_units

        text = source.decode("utf-8", "replace")
        return fbs_chunks(pf), {}, list(fbs_units(path, text).facets)
    if pf.lang == "cmake":
        from server.code.domain.extract.lang_cmake import cmake_chunks, cmake_units

        text = source.decode("utf-8", "replace")
        return cmake_chunks(pf), {}, list(cmake_units(path, text).facets)
    if pf.lang == "doc" and pf.symbols:
        from server.code.domain.extract.lang_markdown import markdown_chunks

        return markdown_chunks(pf, max_chars=max_chunk_chars), {}, []
    return None


def _chunks_for(
    pf: ParsedFile,
    path: str,
    source: bytes,
    max_chunk_chars: int,
    ns_key: tuple[str, str] | None = None,
    parse_error: str = "",
) -> tuple[list[Chunk], dict[int, str], list[Facet]]:
    """The chunks of one parsed file and, for the languages that carry them, the
    deterministic context prefixes (by chunk index) and the facet rows: a reference
    fixture is one head chunk, a SQL test its statement-packed chunks with prefixes and
    facets, a shell test the AST chunker's output coalesced with its SQL islands lifted,
    everything else the AST chunker."""
    from server.settings import settings as _settings

    if pf.lang == REFERENCE_LANG:
        return [reference_chunk(path, source)], {}, []
    if pf.lang == SQL_LANG and _statement_records_on():
        units = sql_units(
            pf,
            target_chars=int(_settings.code_sql_chunk_target_chars),
            max_chars=max_chunk_chars,
        )
        return units.chunks, dict(enumerate(units.prefixes)), units.facets
    if pf.lang == "prisma":
        # One chunk per model / enum block, `creates` facets
        from server.code.domain.extract.lang_prisma import prisma_chunks, prisma_units

        text = source.decode("utf-8", "replace")
        return prisma_chunks(pf), {}, list(prisma_units(path, text).facets)
    if pf.lang in LANE_UNIT_LANGS:
        # No tree — the lane's own symbol spans cut the chunks (one per message /
        # service / define / manifest document)
        from server.code.domain.extract.lane_units import span_chunks

        lane = _lane_module(pf.lang)
        lane_facets = getattr(lane, "facets", None)  # Manifest / crd facets
        return (
            span_chunks(pf, max_chunk_chars, **lane.CHUNK_OPTIONS),
            {},
            list(lane_facets(pf)) if lane_facets else [],
        )
    lane = _lane_chunks(pf, path, source, max_chunk_chars)
    if lane is not None:
        return lane
    chunks = chunk_file(pf, max_chars=max_chunk_chars)
    if pf.lang == "shell" and _shell_islands_on():
        # Coalesce the leftovers, lift the SQL islands; the island statements join the
        # parse's symbols so the symbol stage writes and key-diffs them like the shell
        # functions
        sh = shell_units(
            pf,
            chunks,
            target_chars=int(_settings.code_shell_chunk_target_chars),
        )
        known = {(x.qualified_name, x.id) for x in pf.symbols}
        pf.symbols.extend(x for x in sh.symbols if (x.qualified_name, x.id) not in known)
        return sh.chunks, dict(sh.prefixes), sh.facets
    if pf.lang == "config" and _host_islands_on() and not parse_error:
        # The host's shell islands as facet rows (programs, dialects, statement facets);
        # chunks stay the line windows. Not read again when the parse step failed on them.
        try:
            hu = host_units(pf)
        except ParseTimeout:
            return chunks, {}, []  # its shell ran past the parse deadline: the line windows stand
        if hu is not None:
            return chunks, {}, hu.facets
    if pf.lang == "python" and _py_islands_on():
        # The SQL the file runs as facet rows; chunks stay the AST chunker's (the
        # statement symbols joined the parse already)
        return (
            chunks,
            {},
            [
                *py_sql_units(pf).facets,
                *_env_facets(pf),
                *_py_facets(pf),
                *_py_layout_facets(pf, ns_key),
            ],
        )
    if pf.lang == "python":
        return chunks, {}, [*_env_facets(pf), *_py_facets(pf), *_py_layout_facets(pf, ns_key)]
    if pf.lang == "config" and _is_py_manifest(pf.path):
        return chunks, {}, _py_layout_facets(pf, ns_key)  # Entry points
    if pf.lang == "go":
        # Directives, struct tags and framework markers are facet rows beside the SQL
        # islands'
        from server.code.domain.extract.go_directives import go_directives

        facets = list(go_directives(pf).facets)
        facets.extend(_code_sql_units(pf).facets)
        return chunks, {}, facets
    if pf.lang in CODE_ISLAND_LANGS:
        return chunks, {}, _code_sql_units(pf).facets
    if pf.lang in TS_LANGS:
        # SQL template literals as statement records under the enclosing function; the
        # island symbols join the parse's
        from server.code.domain.extract.lang_ts_islands import ts_units

        tu = ts_units(pf, chunks)
        known = {(x.qualified_name, x.id) for x in pf.symbols}
        pf.symbols.extend(x for x in tu.symbols if (x.qualified_name, x.id) not in known)
        return tu.chunks, tu.prefixes, [*tu.facets, *_route_facets(pf), *_env_facets(pf)]
    if pf.lang in TS_LANGS:
        return chunks, {}, [*_route_facets(pf), *_env_facets(pf)]
    return chunks, {}, []


def _is_py_manifest(path: str) -> bool:
    from server.code.domain.extract.py_layouts import is_manifest_path

    return is_manifest_path(path)


def _py_layout_facets(pf: ParsedFile, ns_key: tuple[str, str] | None) -> list[Any]:
    """The entry points a file declares or serves, as `entry_point` facets — on a
    manifest at each declaration's line, on a `__main__.py` / `if __name__ ==
    "__main__":` module at the guard, on the module a `[project.scripts]` target names
    at the function's line (read off the namespace's cached layout, warmed by the first
    file's edge pass; the heal re-derives) — and a notebook's `notebook_cell` line map."""
    from server.code.domain.extract import py_layouts as pl
    from server.code.domain.extract.py_notebooks import cell_facets, is_notebook_path

    try:
        if pf.lang == "config" or pf.path.endswith("setup.py"):
            text = pf.source.decode("utf-8", "replace")
            out = pl.manifest_facets(pf.path, text) if pl.is_manifest_path(pf.path) else []
        else:
            out = []
        if pf.lang == "python":
            out.extend(pl.script_facets(pf, ordinal=len(out)))
            hit = _PY_LAYOUT_CACHE.get(ns_key) if ns_key is not None else None
            out.extend(pl.module_entry_facets(pf, hit[1] if hit else None, ordinal=len(out)))
            if is_notebook_path(pf.path):
                out.extend(cell_facets(pf.source))
        return out
    except Exception as e:  # noqa: BLE001 — a facet is never worth a lost file
        logger.debug("code ingest: layout facets failed for %s: %s", pf.path, e)
        return []


def _env_facets(pf: ParsedFile) -> list[Any]:
    """The environment variables a file reads, as `env_var` facets at the read's line."""
    from server.code.domain.extract.env_reads import env_facets

    try:
        return env_facets(pf)
    except Exception as e:  # noqa: BLE001 — a facet is never worth a lost file
        logger.debug("code ingest: env facets failed for %s: %s", pf.path, e)
        return []


def _py_facets(pf: ParsedFile) -> list[Any]:
    """The HTTP routes, CLI commands, decorators and async defs of a Python file, as
    facets at the def's (or registration's) line."""
    from server.code.domain.extract.py_routes import py_facets

    try:
        return py_facets(pf)
    except Exception as e:  # noqa: BLE001 — a facet is never worth a lost file
        logger.debug("code ingest: python facets failed for %s: %s", pf.path, e)
        return []


def _route_facets(pf: ParsedFile) -> list[Any]:
    """The HTTP routes a TypeScript / JavaScript file serves, as `http_route` facets at
    the handler's line."""
    from server.code.domain.extract.ts_routes import route_facets

    try:
        return route_facets(pf)
    except Exception as e:  # noqa: BLE001 — a facet is never worth a lost file
        logger.debug("code ingest: route facets failed for %s: %s", pf.path, e)
        return []


def _structural_descriptions(
    ns: str, chunks: list[Chunk], to_insert: list[Chunk], prefixes: dict[int, str]
) -> dict[int, str]:
    """The deterministic context prefix of each chunk being inserted, keyed by its
    position in `to_insert` (the prefixes are keyed by position in `chunks`; the
    content-addressed id joins the two)."""
    if not prefixes:
        return {}
    by_id = {c.chunk_id(ns): prefixes.get(k, "") for k, c in enumerate(chunks)}
    return {i: by_id[c.chunk_id(ns)] for i, c in enumerate(to_insert) if by_id.get(c.chunk_id(ns))}
