"""Edge derivation for one file: the resolver context each language family needs (live
paths, package manifests, workspaces, Python roots, Go modules) and the edges it adds
beside the parser's own."""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from collections.abc import Awaitable, Callable
from typing import Any

from server.code.app.index.ingest_headers import HEADER_EXTS, forget_header, header_context
from server.code.app.index.ingest_parse import (
    _LANE_INCLUDE_LANGS,
    CODE_ISLAND_LANGS,
    LANE_UNIT_LANGS,
    TS_LANGS,
    _code_sql_units,
    _lane_module,
    _py_islands_on,
    _statement_records_on,
)
from server.code.app.index.ingest_resolve import (
    _LIVE_PATHS_CACHE,
    _barrel_targets_of,
    _go_module_index,
    _live_paths,
    _package_json_text,
    _py_roots_for,
    _resolve_lane_includes,
)
from server.code.app.index.ingest_stats import FileIngestStats, error_text
from server.code.domain.extract.edges_treesitter import (
    Edge,
    HeaderDecls,
    collect_call_names,
    extract_edges_and_misses,
)
from server.code.domain.extract.lang_py_islands import py_sql_units
from server.code.domain.extract.lang_shell_islands import drop_program_misses
from server.code.domain.extract.lang_sql_model import SQL_LANG
from server.code.domain.extract.name_scope import drop_test_local_shell_defs, gate_name_table
from server.code.domain.extract.treesitter import ParsedFile
from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_index.ingest")


# A generated table (one initializer thousands of nodes deep) nests past
# Python's default recursion limit in the tree walks of the edge and call-guard
# stages, while its chunks and symbols are fine. The walks are pure reads, so
# the stage simply runs again
# under a deeper limit — process-wide for the retry's duration (a higher
# limit only; Python-to-Python frames cost no C stack since 3.11), restored
# after.
_DEEP_RECURSION_LIMIT = 50_000


async def _with_deep_recursion[T](fn: Callable[[], Awaitable[T]]) -> T:
    """Run `fn`; on RecursionError run it once more under
    `_DEEP_RECURSION_LIMIT`, restoring the limit afterwards."""
    import sys

    try:
        return await fn()
    except RecursionError:
        before = sys.getrecursionlimit()
        if before >= _DEEP_RECURSION_LIMIT:
            raise
        sys.setrecursionlimit(_DEEP_RECURSION_LIMIT)
        try:
            return await fn()
        finally:
            sys.setrecursionlimit(before)


_MAX_PY_INIT_TEXTS = 32  # modules one file plausibly imports from


async def _py_lane_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    lane_context: dict[str, Any] | None,
    name_table: dict[str, list[tuple[str, str, str]]] | None = None,
) -> dict[str, Any] | None:
    """For a Python file, the namespace's present paths as `py_present` — they tell a
    Python definition from another script language's under the shared name scheme, and a
    repository module from a library's. Any other language's context passes through."""
    if pf.lang in TS_LANGS or pf.lang in ("go", "shell"):
        # The present paths, so the repo-wide rung binds only this language's
        # definitions (one cached read per namespace)
        live = await _live_paths(repo, org, ns, pf)
        return lane_context if live is None else {**(lane_context or {}), "present": live}
    if pf.lang != "python":
        return lane_context
    live = await _live_paths(repo, org, ns, pf)
    if live is None:
        return lane_context
    # the `__init__.py` of every package the file imports from, for the re-export chain
    # (`from fastapi import Depends`): cached live texts, one read per package per ten
    # minutes (the package.json cache) Any imported module may re-export
    # (`sqlalchemy.sql .expression`), so every resolved Python include's text, not only
    # the packages'
    inits = [p for p in pf.includes if p.endswith(".py")][:_MAX_PY_INIT_TEXTS]
    texts = {p: t for p in inits if (t := await _package_json_text(repo, org, ns, p))}
    roots = await _py_roots_for(repo, org, ns, pf, live)
    bases = await _py_class_bases(repo, org, ns, pf, name_table or {})
    return {
        **(lane_context or {}),
        "present": live,
        "py_present": live,
        "py_init_texts": texts,
        "py_roots": roots,
        "py_class_bases": bases,
    }


_PY_BASES_DEPTH = 3  # base, grandparent, great-grandparent rows


async def _py_class_bases(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
) -> dict[str, list[str]]:
    """{class qualified name: [base qualified names]} for the classes this file's
    heritage clauses name and their bases, from the `inherits` rows — so
    `self.process()` in a compiler subclass reaches `Compiled.process` two files up, and
    `super().__init__()` a mixin's. One keyed edge read per level, three levels, only
    for files with clauses."""
    if not pf.inherits:
        return {}
    ids: list[str] = []
    for _derived, base in pf.inherits:
        bare = base.rsplit(".", 1)[-1].split("::")[-1]
        ids.extend(c[0] for c in name_table.get(bare, []) if c[2] == "class")
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    for _ in range(_PY_BASES_DEPTH):
        wanted = [i for i in dict.fromkeys(ids) if i not in seen][:400]
        if not wanted:
            break
        seen.update(wanted)
        try:
            rows = await repo.edges_for_symbols(
                org,
                ns,
                wanted,
                direction="callees",
                edge_kinds=["inherits"],
                limit=2000,
            )
        except Exception as e:  # noqa: BLE001 — a missing hierarchy costs recall, not the file
            logger.debug("code ingest: inherits read failed for %s: %s", pf.path, e)
            break
        ids = []
        for r in rows:
            frm, to = str(r.get("from_qualified_name") or ""), str(r.get("to_qualified_name") or "")
            if frm and to and to not in out.setdefault(frm, []):
                out[frm].append(to)
            if r.get("to_symbol_id"):
                ids.append(str(r["to_symbol_id"]))
    return out


async def _derive_edges(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    stats: FileIngestStats,
) -> tuple[list[Any], list[Any]]:
    """This parse's edges and declined sites against the current name table, the class's
    header consulted first (a header read that fails binds what the file alone binds). A
    failing extraction is an index error on the file, never a lost chunk or symbol."""
    header: HeaderDecls | None = None
    if path.endswith(HEADER_EXTS):
        # this header's chunks were just rewritten above (or are about to be
        # read again): the text a source file cached from them is stale
        forget_header(org, ns, path)
    try:
        header = await header_context(repo, org, ns, pf, path, stats)
    except Exception as hdr_exc:  # noqa: BLE001 — degrade to the one-file result
        logger.debug("code ingest: header lookup failed for %s: %s", path, hdr_exc)

    async def _run() -> tuple[list[Edge], list[Any]]:
        await _resolve_lane_includes(repo, org, ns, pf)
        names = collect_call_names(pf, header)
        chains = await _reexport_chains(repo, org, ns, pf)
        names |= {declared for _m, declared in chains.values()}
        name_table = await repo.name_candidates(org, ns, sorted(names)) if names else {}
        # A call site binds only within its language class — a bash `seq` must never
        # resolve to a C++ method of that name.
        name_table = gate_name_table(name_table, pf.lang)
        if pf.lang == "shell":
            name_table = drop_test_local_shell_defs(name_table, path)
        lane_context = await _go_lane_context(repo, org, ns, pf, name_table)
        lane_context = await _py_lane_context(repo, org, ns, pf, lane_context, name_table)
        callee_returns = await _ts_callee_returns(
            repo,
            org,
            ns,
            pf,
            name_table,
            lane_context,
        )
        # CPU work that may parse an imported module: in a thread, so the event loop
        # runs at the parser's reads
        edges, misses = await asyncio.to_thread(
            extract_edges_and_misses,
            pf,
            name_table,
            header,
            callee_returns,
            lane_context,
            chains,
        )
        if pf.lang == "shell":
            misses = drop_program_misses(misses)  # A program is a facet
        if pf.lang in TS_LANGS:
            from server.code.domain.extract.lang_typescript_imports import drop_external_misses

            misses = drop_external_misses(pf, misses, set(pf.includes))
        if pf.lang == SQL_LANG and _statement_records_on():
            edges.extend(await _sql_object_edges(repo, org, ns, pf))
        elif pf.lang == "python" and _py_islands_on():
            edges.extend(await _py_island_edges(repo, org, ns, pf))
        elif pf.lang in CODE_ISLAND_LANGS:
            edges.extend(await _py_island_edges(repo, org, ns, pf))  # same path as Python's islands
        if pf.lang in TS_LANGS:
            edges.extend(await _ts_island_edges(repo, org, ns, pf))
            edges.extend(await _ts_reference_edges(repo, org, ns, pf, chains))
            edges.extend(await _ts_barrel_edges(repo, org, ns, pf))
        if pf.lang == "prisma":
            from server.code.domain.extract.lang_prisma import prisma_edges

            edges.extend(prisma_edges(pf))  # The relation graph
        if pf.lang in LANE_UNIT_LANGS:
            edges.extend(await _lane_site_edges(repo, org, ns, pf))
        if pf.lang in _LANE_INCLUDE_LANGS and pf.symbols:
            edges.extend(await _simple_lane_edges(repo, org, ns, pf))
        return edges, misses

    try:
        return await _with_deep_recursion(_run)
    except Exception as edge_exc:  # noqa: BLE001 — chunks + symbols are already durable
        logger.warning("code ingest: edge extraction failed for %s: %s", path, edge_exc)
        stats.error = "; ".join(x for x in (stats.error, error_text("edges", edge_exc)) if x)
        return [], []


def _angular_relevant(pf: ParsedFile) -> bool:
    from server.code.domain.extract.lang_angular import is_angular_template

    return is_angular_template(pf.path) or b"@Component" in pf.source


async def _angular_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """An external template's (or an inline template's) selector and handler sites bound
    to the components the selectors name and to the owning class's methods."""
    from server.code.domain.extract import lang_angular as ng

    if not _angular_relevant(pf):
        return []
    groups: list[tuple[Any, str, list[tuple[int, str, str]]]] = []  # (src unit, class qn, sites)
    if ng.is_angular_template(pf.path):
        unit = next((s for s in pf.symbols if s.name == ng.TEMPLATE_UNIT), None)
        owner = await _package_json_text(repo, org, ns, ng.owner_path(pf.path))
        cls = ng.owner_class(pf.path, owner) if owner else None
        if cls is None:
            # A template named outside the CLI convention — the component in the same
            # directory whose templateUrl names it
            live = _LIVE_PATHS_CACHE.get((org, ns))
            here = pf.path.rsplit("/", 1)[0] + "/" if "/" in pf.path else ""
            for ts_path in sorted(
                p
                for p in (live[1] if live else set())
                if p.startswith(here) and p.endswith(".ts") and "/" not in p[len(here) :]
            ):
                text = await _package_json_text(repo, org, ns, ts_path)
                if pf.path.rsplit("/", 1)[-1] in text and "@Component" in text:
                    cls = ng.owner_class_from(pf.path, ts_path, text)
                    if cls is not None:
                        break
        if unit is not None and cls is not None:
            groups.append((unit, cls, ng.template_sites(pf.source.decode("utf-8", "replace"))))
    else:
        by_cls: dict[str, list[tuple[int, str, str]]] = {}
        for line, name, kind, cls in ng.inline_template_sites(pf):
            by_cls.setdefault(cls, []).append((line, name, kind))
        for cls, sites in by_cls.items():
            src = next((s for s in pf.symbols if s.qualified_name == cls), None)
            if src is not None:
                groups.append((src, cls, sites))
    if not groups:
        return []
    all_sites = [site for _u, _c, sites in groups for site in sites]
    selector_names = sorted({n for _l, n, k in all_sites if k == "selector"})
    handler_names = sorted({n for _l, n, k in all_sites if k == "handler"})
    rows = await repo.symbols_by_signatures(
        org,
        ns,
        [f"{ng.SELECTOR_SIG}{n}" for n in selector_names],
    )
    selectors = {
        str(r["signature"])[len(ng.SELECTOR_SIG) :]: (str(r["symbol_id"]), str(r["qualified_name"]))
        for r in rows
    }
    table = await repo.name_candidates(org, ns, handler_names) if handler_names else {}
    out: list[Any] = []
    for unit, cls, sites in groups:
        methods = {
            n: [(c[0], c[1]) for c in table.get(n, []) if c[1].startswith(cls + "::")]
            for n in handler_names
        }
        out.extend(ng.template_edges(pf, sites, unit, selectors, methods))
    return out


_MAX_BARREL_FOLLOWS = 64


async def _ts_barrel_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """An `includes` edge at BARREL_CONFIDENCE from this file to every file its resolved
    imports re-export from, one level."""
    from server.code.domain.extract.edges_treesitter import Edge
    from server.code.domain.extract.lang_typescript_imports import BARREL_CONFIDENCE
    from server.code.domain.extract.treesitter import file_symbol_id

    direct = set(pf.includes)
    out: list[Any] = []
    seen: set[str] = set()
    for target in list(pf.includes)[:_MAX_BARREL_FOLLOWS]:
        for beyond in await _barrel_targets_of(repo, org, ns, target):
            if beyond in direct or beyond in seen or beyond == pf.path:
                continue
            seen.add(beyond)
            out.append(
                Edge(
                    "includes",
                    file_symbol_id(pf.path),
                    file_symbol_id(beyond),
                    pf.path,
                    beyond,
                    pf.path,
                    0,
                    BARREL_CONFIDENCE,
                )
            )
    return out


async def _simple_lane_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """The in-file edges of a TLA+ module, a TLC config, a FlatBuffers schema, a CMake
    file or a Markdown document, plus the references it leaves to other files, bound
    once through the namespace name table (bare name → definitions)."""
    if pf.lang == "tla":
        from server.code.domain.extract.lang_tla import tla_external_edges, tla_local_edges

        # it parses the spec again: in a thread, so the event loop runs at the parser's reads
        local, external = await asyncio.to_thread(tla_local_edges, pf)
        if not external:
            return local
        table = await repo.name_candidates(org, ns, sorted({n for _f, n, _l in external})[:400])
        return [*local, *tla_external_edges(pf, external, table)]
    if pf.lang == "tla_config":
        from server.code.domain.extract.lang_tla import tlc_edges, tlc_units

        names = sorted({n for _r, n, _l in tlc_units(pf.path, pf.source).checked})
        table = await repo.name_candidates(org, ns, names) if names else {}
        return tlc_edges(pf, table)
    if pf.lang == "flatbuffers":
        from server.code.domain.extract.lang_flatbuffers import fbs_external_edges, fbs_local_edges

        local, external = fbs_local_edges(pf)
        if not external:
            return local
        table = await repo.name_candidates(org, ns, sorted({n for _f, n, _l in external})[:400])
        return [*local, *fbs_external_edges(pf, external, table)]
    if pf.lang == "cmake":
        from server.code.domain.extract.lang_cmake import cmake_external_edges, cmake_local_edges

        local, external = cmake_local_edges(pf)
        if not external:
            return local
        names = sorted({t for t, _d, _l in external} | {d for _t, d, _l in external})[:400]
        table = await repo.name_candidates(org, ns, names)
        return [*local, *cmake_external_edges(pf, external, table)]
    if pf.lang == "doc":
        from server.code.domain.extract.lang_markdown import (
            doc_reference_edges,
            doc_reference_sites,
        )

        sites = doc_reference_sites(pf)
        if not sites:
            return []
        names = sorted({n.rsplit("::", 1)[-1] for _f, n, _l in sites})[:400]
        table = await repo.name_candidates(org, ns, names)
        return doc_reference_edges(pf, sites, table)
    return []


async def _go_external_registrations(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    lane_context: dict[str, Any],
) -> None:
    """`Register<Svc>Server(server, api)` with `api` typed to another package's struct
    (`api, err := queries.New(cfg)`). The struct and its methods are rows this file
    cannot see: one return-type read types the identifier, one `name_candidates` read
    finds the struct row (the type's package alias picking among namesakes), one
    `members_of` read lists its methods, and the rpc rows of those names join the
    service table. Only for files with such a registration."""
    from server.code.domain.extract.edges_treesitter import callee_targets
    from server.code.domain.extract.go_grpc import (
        external_registrations,
        external_struct,
        has_identifier_registration,
    )
    from server.code.domain.extract.lang_go import go_import_aliases

    if not has_identifier_registration(pf):
        return
    targets = callee_targets(pf, name_table, lane_context)
    returns: dict[str, str] = {}
    if targets:
        types = await repo.return_types_for_qualified(org, ns, sorted(set(targets.values())))
        returns = {c: types[qn] for c, qn in targets.items() if qn in types}
    impls = external_registrations(pf, returns)
    if not impls:
        return
    aliases = go_import_aliases(pf)
    table = await repo.name_candidates(org, ns, sorted({i.type_name for i in impls})[:50])
    resolved: list[dict[str, Any]] = []
    for impl in impls:
        found = external_struct(table, impl, aliases.get(impl.type_alias, ""))
        if found is None:
            continue
        struct_id, struct_qn = found
        members = await repo.members_of(org, ns, [struct_qn], limit=200)
        methods = [
            (
                str(m["name"]),
                str(m["symbol_id"]),
                str(m["qualified_name"]),
                int(m.get("start_line") or 0),
            )
            for m in members
            if m.get("kind") in ("function", "method")  # scip rows say method
        ]
        resolved.append(
            {
                "service": impl.service,
                "service_alias": impl.service_alias,
                "struct_id": struct_id,
                "struct_qn": struct_qn,
                "line": impl.line,
                "methods": methods,
            }
        )
    if not resolved:
        return
    lane_context["go_grpc_external"] = resolved
    names = sorted(
        {m[0] for r in resolved for m in r["methods"]} | {r["service"] for r in resolved}
    )
    extra = await repo.name_candidates(org, ns, names[:400])
    merged = dict(lane_context.get("go_grpc_table", {}))
    for k, v in extra.items():
        merged[k] = [*merged.get(k, []), *[c for c in v if c not in merged.get(k, [])]]
    lane_context["go_grpc_table"] = merged


async def _go_service_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    lane_context: dict[str, Any],
    name_table: dict[str, list[tuple[str, str, str]]] | None = None,
) -> None:
    """The proto service and rpc rows a handler file implements (one `name_candidates`
    read for the service and method names, only for a file that embeds or registers a
    service) and the live paths under the package directory a `//go:embed` file names
    (one keyed read, only for a file with the directive)."""

    from server.code.domain.extract.go_directives import go_directives
    from server.code.domain.extract.go_grpc import grpc_wanted_names

    wanted = grpc_wanted_names(pf)
    if wanted:
        lane_context["go_grpc_table"] = await repo.name_candidates(org, ns, wanted[:400])
    await _go_external_registrations(repo, org, ns, pf, name_table or {}, lane_context)
    if go_directives(pf).embeds:
        pkg_dir = posixpath.dirname(pf.path)
        prefix = f"{pkg_dir}/" if pkg_dir else ""
        lane_context["go_embed_paths"] = await repo.live_paths_under(
            org,
            ns,
            prefix,
            limit=_EMBED_PATHS_CAP,
        )


_EMBED_PATHS_CAP = 4000


async def _go_lane_context(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
) -> dict[str, Any] | None:
    """Which repository directory each import alias of a Go file names (through the module index),
    and one keyed read of the rows the file's derivation may consult: the concrete methods an
    interface declared here could be satisfied by, and the struct fields its receiver chains walk.
    None for any other language."""
    if pf.lang != "go":
        return None
    from server.code.domain.extract.go_implementors import (
        candidate_method_ids,
        go_clauses_of,
        implementor_type_names,
        struct_embeds,
        type_alias_target,
        type_element,
        value_type,
    )
    from server.code.domain.extract.lang_go import go_package_context

    lane_context: dict[str, Any] = {
        "go_packages": go_package_context(pf, await _go_module_index(repo, org, ns)),
    }
    lane_context["go_clauses"] = go_clauses_of(lane_context["go_packages"], name_table)
    await _go_service_context(repo, org, ns, pf, lane_context, name_table)
    # The rows of the types whose methods satisfy an interface declared here — one keyed
    # read, only for files that declare interfaces
    type_names = implementor_type_names(pf, name_table)
    if type_names:
        from server.code.domain.extract.name_scope import gate_name_table

        lane_context["go_type_rows"] = gate_name_table(
            await repo.name_candidates(org, ns, type_names[:400]),
            "go",
        )
    wanted_ids = candidate_method_ids(pf, name_table)
    if wanted_ids:
        rows = await repo.symbols_by_ids(org, ns, wanted_ids[:2000])
        lane_context["go_method_sigs"] = {
            str(r["symbol_id"]): str(r.get("signature") or "") for r in rows
        }
        # The types each bound struct embeds, from its row
        embeds = {
            str(r["qualified_name"]): struct_embeds(str(r.get("signature") or ""))
            for r in rows
            if r.get("kind") == "struct"
        }
        lane_context["go_struct_embeds"] = {qn: e for qn, e in embeds.items() if e}
        # This package's consts / vars by declared type, and the element type of every
        # named container the file's types name
        own = next((s.qualified_name.partition("::")[0] for s in pf.symbols), "")
        lane_context["go_value_types"] = {
            str(r["name"]): value_type(str(r.get("signature") or ""))
            for r in rows
            if r.get("kind") in ("const", "var")
            and str(r["qualified_name"]).partition("::")[0] == own
            and value_type(str(r.get("signature") or ""))
        }
        lane_context["go_type_elems"] = {
            str(r["name"]): type_element(str(r.get("signature") or ""))
            for r in rows
            if r.get("kind") == "type" and type_element(str(r.get("signature") or ""))
        }
        # `type Dynamic = Variant` — the typed rung follows it
        lane_context["go_type_aliases"] = {
            str(r["qualified_name"]): type_alias_target(str(r.get("signature") or ""))
            for r in rows
            if r.get("kind") == "type" and type_alias_target(str(r.get("signature") or ""))
        }
    return lane_context


async def _ts_callee_returns(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    name_table: dict[str, list[tuple[str, str, str]]],
    lane_context: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The return types of the callees this file's bindings wait on (`const client =
    createClient(opts)`), one keyed read of the symbols they bind to; {} for a file with
    no such binding."""
    if pf.lang not in TS_LANGS and pf.lang not in ("python", "go"):
        return {}
    from server.code.domain.extract.edges_treesitter import callee_targets

    targets = callee_targets(pf, name_table, lane_context)
    if not targets:
        return {}
    types = await repo.return_types_for_qualified(org, ns, sorted(set(targets.values())))
    out = {callee: types[qn] for callee, qn in targets.items() if qn in types}
    if pf.lang in ("go", "python") and out:
        # One more round for the callees a now-typed receiver resolves (`batch, _ :=
        # conn.PrepareBatch()` → `batch.Append`), one extra keyed read only when the
        # round finds new targets
        more = {
            c: qn
            for c, qn in callee_targets(pf, name_table, lane_context, out).items()
            if c not in out
        }
        wanted = sorted({qn for qn in more.values() if qn not in types})
        if wanted:
            types.update(await repo.return_types_for_qualified(org, ns, wanted))
        out.update({c: types[qn] for c, qn in more.items() if qn in types})
    return out


async def _reexport_chains(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
) -> dict[str, tuple[str, str]]:
    """{local import name: (declaring module scope, declared name)} for the names this
    file imports whose module re-exports rather than declares them — followed through
    named and star re-exports to the declaration over the cached live texts. Empty
    outside the TypeScript lane or without the live set."""
    if pf.lang not in TS_LANGS or pf.tree is None:
        return {}
    live = _LIVE_PATHS_CACHE.get((org, ns))
    if live is None:
        return {}
    from server.code.domain.extract.lang_python import module_scope
    from server.code.domain.extract.lang_typescript_imports import import_bindings
    from server.code.domain.extract.ts_reexports import resolve_export, resolve_spec

    present = live[1]

    async def _text(path: str) -> str:
        return await _package_json_text(repo, org, ns, path)

    out: dict[str, tuple[str, str]] = {}
    for local, imported, spec in _import_names(pf, import_bindings(pf)):
        target = resolve_spec(pf.path, spec, present)
        if target is None:
            continue
        hit = await resolve_export(target, imported, present, _text)
        if hit is not None and (hit[0] != target or hit[1] != imported):
            out[local] = (module_scope(hit[0]), hit[1])
    return out


# A clause stops at the next `import` keyword: an import with no `from` would otherwise
# search on to the end of the file from each one, quadratic in a file of them. It runs on
# the text without comments, whose `import` would end a clause early; strings are kept
# (a `//` in one is no comment), an unclosed quote to the end of its line.
_STRING_RE = re.compile(
    r"""\"(?:[^"\\\n]|\\.)*+"?|'(?:[^'\\\n]|\\.)*+'?|`(?:[^`\\]|\\.)*+`?""", re.S
)
_OPENER_RE = re.compile(r"""["'`]|//|/\*""")


def _without_comments(text: str) -> str:
    """`text` with its comments blanked and its strings kept, in one pass. A `/*` or `//`
    after a backslash is a regex's (`/^\\/*/`), and a `/*` with no `*/` after it is text,
    as `ts_config.strip_comments` reads it, with no later one closing either."""
    out: list[str] = []
    i = 0
    closed = True
    while m := _OPENER_RE.search(text, i):
        j, tok = m.start(), m.group()
        if tok[0] == "/" and text[j - 1 : j] == "\\":
            out.append(text[i : j + 1])
            i = j + 1
        elif tok == "//":
            end = text.find("\n", j)
            out.append(text[i:j] + " ")
            i = len(text) if end < 0 else end
        elif tok == "/*":
            end = text.find("*/", j + 2) if closed else -1
            closed = end >= 0
            out.append(text[i:j] + " " if closed else text[i : j + 1])
            i = end + 2 if closed else j + 1
        else:
            string = _STRING_RE.match(text, j)
            end = string.end() if string else j + 1
            out.append(text[i:end])
            i = end
    out.append(text[i:])
    return "".join(out)


_IMPORT_CLAUSE_RE = re.compile(
    r"""(?<![\w$])import\s++((?:(?!(?<![\w$])import(?![\w$]))[^;'"])+?)(?<=\S)\s+from\s*["']([^"']+)["']"""
)


def _import_names(pf: ParsedFile, bindings: dict[str, str]) -> list[tuple[str, str, str]]:
    """(local name, imported name | 'default', specifier) per import binding;
    namespace imports are skipped (their members bind through the root)."""
    text = _without_comments(pf.source.decode("utf-8", "replace"))
    out: list[tuple[str, str, str]] = []
    for m in _IMPORT_CLAUSE_RE.finditer(text):
        clause, spec = m.group(1), m.group(2)
        dflt = re.match(r"^\s*([A-Za-z_$][\w$]*)\s*(?:,|$)", clause)
        if dflt and bindings.get(dflt.group(1)) == spec:
            out.append((dflt.group(1), "default", spec))
        inner = re.search(r"\{([^{}]*)\}", clause)  # `[^}]` would rescan from every `{`
        if inner:
            for part in inner.group(1).split(","):
                part = part.strip()
                if not part or part.startswith("type "):
                    continue
                if " as " in part:
                    name, _, alias = (x.strip() for x in part.partition(" as "))
                else:
                    name = alias = part
                local = alias or name
                if re.fullmatch(r"[A-Za-z_$][\w$]*", local) and bindings.get(local) == spec:
                    out.append((alias or name, name, spec))
    return out


async def _ts_reference_edges(
    repo: CodeRepo,
    org: str,
    ns: str,
    pf: ParsedFile,
    chains: dict[str, tuple[str, str]] | None = None,
) -> list[Any]:
    """Types in type positions → `uses_type`, JSX component elements → `calls`, bound by
    the extractor's ladder over the script- class table; misses are not recorded (a
    library type is the norm)."""
    from server.code.domain.extract.lang_sfc import is_sfc_path, markup_sites
    from server.code.domain.extract.lang_typescript_imports import import_bindings
    from server.code.domain.extract.lang_typescript_refs import (
        _MEMBER_CONTAINER_KINDS,
        member_read_edges,
        member_read_sites,
        reference_edges,
        reference_sites,
        value_reference_edges,
        value_reference_sites,
    )
    from server.code.domain.extract.lang_vue import is_vue_path, template_edges, template_sites

    sites = reference_sites(pf)
    bindings = import_bindings(pf)
    # `Obj.member` reads of an imported or same-file container
    keep = set(bindings) | {x.name for x in pf.symbols if x.kind in _MEMBER_CONTAINER_KINDS}
    msites = member_read_sites(pf, keep)
    # Names used as values — passed, stored, rendered, returned
    vkeep = set(bindings) | {x.name for x in pf.symbols}
    vsites = value_reference_sites(pf, vkeep)
    template: list[tuple[int, str, str]] = []
    if is_vue_path(pf.path):
        # The template's elements and handlers
        template = template_sites(pf, bindings)
    elif is_sfc_path(pf.path):
        template = markup_sites(pf, bindings)
    angular = await _angular_edges(repo, org, ns, pf)
    if not sites and not template and not msites and not vsites and not _angular_relevant(pf):
        return []
    # A default-imported or barrel-aliased single-file component is named by the
    # importer, not by its unit — bind through the module
    from server.code.domain.extract.lang_sfc import import_target_edges, import_targets

    live = _LIVE_PATHS_CACHE.get((org, ns))
    present = live[1] if live is not None else set(pf.includes)

    async def _text(path: str) -> str:
        return await _package_json_text(repo, org, ns, path)

    targets = await import_targets(pf, present, _text)
    names = sorted(
        {n for _l, n, _k in (*sites, *template)}
        | {q.split("::")[-1] for q in targets.values()}
        | {declared for _m, declared in (chains or {}).values()}
        | {o for _l, o, _m in msites}
        | {m for _l, _o, m in msites}
        | {n for _l, n in vsites}
    )
    table = gate_name_table(await repo.name_candidates(org, ns, names), pf.lang)
    edges = reference_edges(pf, sites, table, bindings, chains) if sites else []
    if msites:
        edges.extend(member_read_edges(pf, msites, table, bindings, chains))
    if vsites:
        known_v = {(e.from_symbol_id, e.to_symbol_id, e.line) for e in edges}
        edges.extend(
            e
            for e in value_reference_edges(pf, vsites, table, bindings, chains)
            if (e.from_symbol_id, e.to_symbol_id, e.line) not in known_v
        )
    if template:
        edges.extend(template_edges(pf, template, table, bindings))
    if targets:
        known = {(e.from_symbol_id, e.to_symbol_id, e.line) for e in edges}
        edges.extend(
            e
            for e in import_target_edges(pf, [*sites, *template], targets, table)
            if (e.from_symbol_id, e.to_symbol_id, e.line) not in known
        )
    edges.extend(angular)
    return edges


async def _lane_site_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """The reference sites of a tree-less lane (an rpc's request type, a template's
    `include "x"`) bound by `lane_units.bind_sites` over the lane's gated name table —
    same-file unique at 1.0, repository-unique at 0.6, nothing when ambiguous."""
    from server.code.domain.extract.lane_units import bind_sites

    lane = _lane_module(pf.lang)
    sites = lane.reference_sites(pf)
    if not sites:
        return []
    names = sorted({n for _l, n, _k in sites})
    table = gate_name_table(await repo.name_candidates(org, ns, names), pf.lang)
    return bind_sites(pf, sites, table, lane.SITE_RULES)


async def _ts_island_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """The island statements' table / function edges over the SQL-gated name table, from
    the statement and its enclosing function."""
    from server.code.domain.extract.lang_sql_objects import reference_names
    from server.code.domain.extract.lang_ts_islands import island_edges, island_records

    statements, scopes, _islands = island_records(pf)
    if not statements:
        return []
    names = reference_names(statements)
    table = await repo.name_candidates(org, ns, sorted(names)) if names else {}
    gated = gate_name_table(table, SQL_LANG)
    scope_dialects = await _scope_dialects_for(repo, org, ns, gated)
    return island_edges(
        pf,
        statements,
        scopes,
        gated,
        rw_kinds=True,
        scope_dialects=scope_dialects,
    )


async def _scope_dialects_for(
    repo: CodeRepo,
    org: str,
    ns: str,
    table: dict[str, list[tuple[str, str, str]]],
) -> dict[str, str]:
    """{module scope: dialect} for the defining files of every name the table holds MORE
    than one definition of — the file-level `dialect` facet that `narrow_by_dialect`
    reads. One id → path read, then one facet read per path; a file whose tables are all
    unambiguous pays nothing."""
    from server.code.domain.extract.lang_python import module_scope

    ambiguous_ids = [c[0] for cands in table.values() if len(cands) > 1 for c in cands]
    if not ambiguous_ids:
        return {}
    paths = await repo.symbol_paths_for_ids(org, ns, ambiguous_ids[:200])
    out: dict[str, str] = {}
    for path in sorted(set(paths.values())):
        for f in await repo.live_facets_for_path(org, ns, path):
            if str(f["facet_kind"]) == "dialect" and int(f["ordinal"]) == 0:
                out[module_scope(path)] = str(f["name"])
                break
    return out


async def _sql_object_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """Statement → table / view / function edges for a SQL file — its own definitions
    first, then repository-unique ones from the name table gated to SQL's object kinds.
    No misses: a query file against a live database references tables nobody creates in
    the tree."""
    from server.code.domain.extract.lang_python import module_scope
    from server.code.domain.extract.lang_sql import split_file
    from server.code.domain.extract.lang_sql_jinja import jinja_edges
    from server.code.domain.extract.lang_sql_objects import reference_names, sql_edges

    statements, ju = split_file(pf.path, pf.source.decode("utf-8", "replace"))
    names = reference_names(statements) | set(ju.refs) | set(ju.macro_calls)
    col = _ColumnAsk.for_statements(statements)
    names |= col.names
    table = await repo.name_candidates(org, ns, sorted(names)) if names else {}
    gated = gate_name_table(table, SQL_LANG)
    edges = sql_edges(pf.path, module_scope(pf.path), statements, pf.symbols, gated, rw_kinds=True)
    if ju.dbt:
        edges.extend(jinja_edges(pf.path, ju, gated, rw_kinds=True))
    edges.extend(col.edges(pf, statements, gated))
    return edges


class _ColumnAsk:
    """The column-lineage share of a SQL file's edge pass: the column names to ask the
    name table for (the file's bound references plus the `<table>.<identifier>`
    hypotheses of its single-table statements), and the statement → column edges once
    the table answered."""

    def __init__(self, known: dict[str, set[str]], hypotheses: set[str], names: set[str]) -> None:
        self.known, self.hypotheses, self.names = known, hypotheses, names

    @classmethod
    def for_statements(cls, statements: list[Any]) -> _ColumnAsk:
        from server.code.domain.extract.lang_sql_columns import (
            column_hypotheses,
            column_lineage_on,
            column_names,
            file_columns,
        )

        if not column_lineage_on() or not statements:
            return cls({}, set(), set())
        known = file_columns(statements)
        hyps = column_hypotheses(statements, known)
        return cls(known, hyps, hyps | column_names(statements, known))

    def edges(self, pf: ParsedFile, statements: list[Any], gated: dict[str, Any]) -> list[Any]:
        from server.code.domain.extract.lang_python import module_scope
        from server.code.domain.extract.lang_sql_columns import (
            column_edges,
            known_from_name_table,
            merge_known,
        )

        if not self.names and not self.known:
            return []
        known = merge_known(self.known, known_from_name_table(gated, self.hypotheses))
        return column_edges(
            pf.path,
            module_scope(pf.path),
            statements,
            known,
            pf.symbols,
            gated,
            rw_kinds=True,
        )


async def _py_island_edges(repo: CodeRepo, org: str, ns: str, pf: ParsedFile) -> list[Any]:
    """Statement → table / view edges for the SQL a Python file runs, resolved like a
    SQL file's (own definitions first, then repository-unique objects gated to SQL's
    kinds)."""
    from server.code.domain.extract.lang_python import module_scope
    from server.code.domain.extract.lang_sql_objects import reference_names, sql_edges

    statements = (
        py_sql_units(pf).statements if pf.lang == "python" else _code_sql_units(pf).statements
    )
    if not statements:
        return []
    col = _ColumnAsk.for_statements(statements)
    names = reference_names(statements) | col.names
    table = await repo.name_candidates(org, ns, sorted(names)) if names else {}
    gated = gate_name_table(table, SQL_LANG)
    edges = sql_edges(pf.path, module_scope(pf.path), statements, pf.symbols, gated, rw_kinds=True)
    edges.extend(col.edges(pf, statements, gated))
    return edges
