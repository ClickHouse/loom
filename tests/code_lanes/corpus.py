"""The SQL / shell lanes corpus: what the code plane derives
from every fixture under `tests/fixtures/code_lanes/`, as one JSON the unit
gate compares against.

    python -m tests.code_lanes.corpus            # print the summary
    python -m tests.code_lanes.corpus --write    # regenerate expected.json

Per file: the language, the symbols by kind, the facets by kind, the
include targets; corpus-wide: the edges by (from path, edge kind, target
kind) with a name table built from the corpus itself — the same
derivation `ingest_file` makes, minus the store. A change to any lane
(splitter, directive table, client table, shell detection, host islands,
object symbols, Jinja) that moves a number here is a change to what a
namespace holds, and the gate makes it a visible, reviewed one.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import posixpath
import sys
from collections import Counter
from collections.abc import Iterator
from typing import Any

ROOT = posixpath.join(
    posixpath.dirname(posixpath.dirname(posixpath.abspath(__file__))), "fixtures", "code_lanes"
)
EXPECTED = posixpath.join(ROOT, "expected.json")


_LANE_SETTINGS = (
    "code_sql_statement_records",
    "code_shell_islands",
    "code_host_islands",
    "code_py_sql_islands",
    "code_sql_column_lineage",
)


@contextlib.contextmanager
def lane_settings_on() -> Iterator[None]:
    """The lane settings on for the block, then as they were: other tests
    assert a SQL file has no symbols with the flag off. A name that is no
    longer a settings field fails here instead of switching nothing."""
    from server.settings import settings

    missing = sorted(set(_LANE_SETTINGS) - set(type(settings).model_fields))
    if missing:
        raise RuntimeError(f"_LANE_SETTINGS names settings that no longer exist: {missing}")
    before = settings.model_dump(include=set(_LANE_SETTINGS))
    try:
        for k in _LANE_SETTINGS:
            setattr(settings, k, True)
        yield
    finally:
        for k, v in before.items():
            setattr(settings, k, v)


@dataclasses.dataclass(frozen=True)
class Lane:
    """One fixture directory and the language the code plane gives it.

    `manifests` maps a basename or suffix to the language of the lane's
    non-source files (a module file, a chart's values). `inert` names the
    fixtures that derive nothing, each with the reason: a support file or a
    limit of the lane."""

    prefix: str
    lang: str
    covers: str
    limits: str = ""
    manifests: tuple[tuple[str, frozenset[str]], ...] = ()
    inert: tuple[tuple[str, str], ...] = ()

    def langs_for(self, path: str) -> frozenset[str]:
        name = posixpath.basename(path)
        for pattern, langs in self.manifests:
            if name == pattern or name.endswith(pattern):
                return langs
        return frozenset({self.lang})


_CONFIG = frozenset({"config"})

LANES: tuple[Lane, ...] = (
    Lane(
        "cmake/",
        "cmake",
        "targets, options, add_subdirectory includes, test targets and the "
        "sources a target builds, across three directories",
    ),
    Lane(
        "fbs/",
        "flatbuffers",
        "namespaces, an include, tables, structs, an enum, a union, "
        "a service and the cross-file type uses",
    ),
    Lane(
        "go/",
        "go",
        "packages, receivers, interfaces and their satisfaction, embedding, build "
        "constraints, an external test package, internal packages, a two-module "
        "monorepo with a replace directive, and sqlc's generated query constants",
        manifests=(("go.mod", _CONFIG),),
    ),
    Lane(
        "helm/",
        "gotemplate",
        "a chart with a dependency, defines and includes, manifest documents and NOTES.txt",
        limits="an include outside any define or YAML document has no symbol to hang an "
        "edge from, so NOTES.txt derives nothing",
        manifests=(("Chart.yaml", _CONFIG), ("values.yaml", _CONFIG)),
        inert=(("helm/mychart/templates/NOTES.txt", "an include outside any define"),),
    ),
    Lane(
        "hosts/",
        "config",
        "build stages, make targets, CI jobs and the programs and scripts their steps run",
        limits="an include names the script path as written; the corpus holds none of them",
    ),
    Lane(
        "java/",
        "java",
        "a record, a Spring / JDBC repository and the SQL it runs, a generic interface, a "
        "generic annotated class with a constructor and delegating calls",
        limits="`extends` / `implements` derive no inherits edge",
    ),
    Lane(
        "js/", "javascript", "CommonJS exports, prototype methods, an IIFE and function expressions"
    ),
    Lane(
        "md/",
        "doc",
        "sections, cross-document links, backticked references to the TLA+ spec's names, "
        "and fenced code whose names are not references",
    ),
    Lane("prisma/", "prisma", "models, relations, an enum and attributes of the vendored schema"),
    Lane("proto/", "proto", "a package, an import, messages, an enum, a service and its RPCs"),
    Lane(
        "python/",
        "python",
        "src, legacy, namespace, vendored, notebook, stub and plugin layouts; absolute, "
        "relative, aliased and re-exported imports; decorators, dataclasses, protocols, overrides and "
        "properties; a Django-style site's routes; SQL through DB-API, SQLAlchemy and a "
        "test harness client",
        manifests=(("pyproject.toml", _CONFIG),),
        inert=(
            ("python/layouts/legacy/python/legacy/__init__.py", "package marker"),
            ("python/layouts/srcpkg/lib/srcpkg/plugins/__init__.py", "package marker"),
            ("python/layouts/vendored/src/app/__init__.py", "package marker"),
            ("python/members/__init__.py", "package marker"),
            ("python/members/cli/__init__.py", "package marker"),
            ("python/members/svc/__init__.py", "package marker"),
        ),
    ),
    Lane(
        "rust/",
        "rust",
        "modules, structs, enums, impl blocks, use paths and calls across files; a trait "
        "and its impl, a declarative macro and an async function",
    ),
    Lane(
        "shell/",
        "shell",
        "sourced libraries, functions, bats tests, an extensionless test file whose name "
        "carries spaces and quotes, and SQL islands with their dialects",
        limits="a `$VAR/…` source path the corpus cannot settle stays as written",
    ),
    Lane(
        "sql/",
        "sql",
        "ClickHouse tests and a Jinja template; goose, sqlc, dbmate, Liquibase, Flyway, "
        "Prisma and Sqitch migrations; dbt models and macros; GO / DELIMITER / slash "
        "terminators; psql \\ir includes; ClickHouse DDL of the vendored schema; a "
        "materialized view into a rollup, a view and a CTE",
        limits="a psql `\\ir` include names the file as written",
    ),
    Lane(
        "tla/",
        "tla",
        "modules, constants, variables, operators, an EXTENDS of a sibling module and a TLC model config",
        manifests=((".cfg", frozenset({"tla_config"})),),
    ),
    Lane(
        "ts/",
        "typescript",
        "classes, overloads, dynamic imports, React components, API routes, a test suite, "
        "barrels and a package.json exports table, SQL in template literals",
        manifests=(("package.json", _CONFIG),),
    ),
    Lane(
        "vue/",
        "javascript",
        "single-file components in JavaScript and TypeScript, props, emits, a composable "
        "and the template's component and handler references",
        manifests=((".vue", frozenset({"javascript", "typescript"})),),
    ),
)


def lane_of(path: str) -> Lane:
    return next(lane for lane in LANES if path.startswith(lane.prefix))


def fixture_paths() -> list[str]:
    """Every corpus file. Bytecode caches are skipped: a fixture module
    imported once would otherwise join the corpus as a `.pyc`."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            rel = posixpath.relpath(posixpath.join(dirpath, f), ROOT)
            if rel in ("expected.json", "NOTICE.md"):
                continue
            out.append(rel)
    return sorted(out)


def summarize() -> dict[str, Any]:
    """{files: {path: {...}}, edges: {"path | kind | target_kind": n}}."""
    with lane_settings_on():
        return _summarize()


def _summarize() -> dict[str, Any]:
    from server.code.app.index.ingest_parse import parse_file
    from server.code.domain.extract import py_layouts as pl
    from server.code.domain.extract.chunker import chunk_file
    from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
    from server.code.domain.extract.lane_units import bind_sites
    from server.code.domain.extract.lang_gotemplate import (
        chart_dependency_candidates,
        is_chart_manifest,
    )
    from server.code.domain.extract.lang_hosts import host_units
    from server.code.domain.extract.lang_prisma import prisma_edges, prisma_units
    from server.code.domain.extract.lang_py_islands import py_sql_units
    from server.code.domain.extract.lang_python import module_scope
    from server.code.domain.extract.lang_rust import resolve_uses
    from server.code.domain.extract.lang_shell_islands import (
        drop_program_misses,
        settle_cwd_includes,
        shell_units,
    )
    from server.code.domain.extract.lang_sql import split_file, sql_units
    from server.code.domain.extract.lang_sql_jinja import jinja_edges
    from server.code.domain.extract.lang_sql_model import SQL_LANG
    from server.code.domain.extract.lang_sql_objects import reference_names, sql_edges
    from server.code.domain.extract.lang_ts_islands import (
        island_edges,
        island_records,
        ts_units,
    )
    from server.code.domain.extract.lang_typescript_imports import (
        BARREL_CONFIDENCE,
        barrel_targets,
        drop_external_misses,
        exports_candidates,
        import_bindings,
        manifest_view,
        package_dirs_for,
        resolve_imports,
        unresolved_package_specs,
    )
    from server.code.domain.extract.lang_typescript_refs import reference_edges, reference_sites
    from server.code.domain.extract.lang_vue import is_vue_path, template_edges, template_sites
    from server.code.domain.extract.name_scope import drop_test_local_shell_defs, gate_name_table
    from server.code.domain.extract.py_notebooks import cell_facets, is_notebook_path

    ts_langs = {"typescript", "javascript"}

    # The roots and entry points the fixture manifests declare,
    # as `ingest._py_layout` reads them from the live chunks
    all_paths = fixture_paths()
    layout = pl.layout_from_texts(
        {
            p: open(posixpath.join(ROOT, p), errors="replace").read()
            for p in all_paths
            if pl.is_manifest_path(p)
        },
        set(all_paths),
    )

    parsed: dict[str, Any] = {}
    files: dict[str, Any] = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    scope_dialects: dict[str, str] = {}
    for rel in fixture_paths():
        # the corpus root stands for the repository root: `hosts/.github/…`
        # and `sql/dbt/models/…` keep the directory shape the lanes key on
        source = open(posixpath.join(ROOT, rel), "rb").read()
        pf = parse_file(rel, source)
        if pf is None:
            files[rel] = {"lang": ""}
            continue
        parsed[rel] = pf
        if pf.lang == "shell":
            # A `?cwd:` alternate is an include only when the
            # corpus holds the file, as the ingest settles it against live paths
            pf.includes = settle_cwd_includes(pf.includes, set(fixture_paths()))
        facets: list[Any] = []
        if pf.lang == SQL_LANG:
            facets = sql_units(pf).facets
        elif pf.lang == "shell":
            facets = shell_units(pf, chunk_file(pf)).facets
        elif pf.lang == "config":
            hu = host_units(pf)
            facets = (
                hu.facets if hu else (pl.manifest_facets(rel, source.decode("utf-8", "replace")))
            )
        elif pf.lang == "python":
            # SQL islands, routes / commands /
            # decorators, entry points and notebook cells
            from server.code.domain.extract.py_routes import py_facets

            facets = [
                *py_sql_units(pf).facets,
                *py_facets(pf),
                *pl.script_facets(pf),
                *pl.module_entry_facets(pf, layout),
            ]
            if rel.endswith("setup.py"):
                facets.extend(pl.manifest_facets(rel, source.decode("utf-8", "replace")))
            if is_notebook_path(rel):
                facets.extend(cell_facets(pf.source))
        elif pf.lang in ("go", "java"):
            facets = _code_units(pf).facets
        elif pf.lang in ts_langs:
            # The island statement symbols join the parse's,
            # as `ingest._chunks_for` makes them
            tu = ts_units(pf, chunk_file(pf))
            pf.symbols.extend(tu.symbols)
            facets = tu.facets
        elif pf.lang == "prisma":
            facets = prisma_units(rel, source.decode("utf-8", "replace")).facets
        elif pf.lang == "tla_config":
            from server.code.domain.extract.lang_tla import tlc_units

            facets = tlc_units(rel, source).facets
        elif pf.lang == "flatbuffers":
            from server.code.domain.extract.lang_flatbuffers import fbs_units

            facets = fbs_units(rel, source.decode("utf-8", "replace")).facets
        elif pf.lang == "cmake":
            from server.code.domain.extract.lang_cmake import cmake_units

            facets = cmake_units(rel, source.decode("utf-8", "replace")).facets
        if pf.lang == SQL_LANG:
            # The file-level dialect the island edges narrow by
            for fct in facets:
                if fct.kind == "dialect" and fct.ordinal == 0:
                    scope_dialects[module_scope(rel)] = fct.name
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
        files[rel] = {
            "lang": pf.lang,
            "symbols": dict(sorted(Counter(s.kind for s in pf.symbols).items())),
            "facets": dict(sorted(Counter(f.kind for f in facets).items())),
            "includes": list(pf.includes),
        }
        if pf.lang in ts_langs or pf.lang == "rust":
            # the resolved paths land here after the edge pass; the summary
            # records the RAW specifiers too so a resolver change is visible
            files[rel]["specifiers"] = list(pf.includes)
    kind_of = {sid: kind for cands in name_table.values() for sid, _qn, kind in cands}
    returns_by_qn = {
        s.qualified_name: s.return_type
        for pf in parsed.values()
        for s in pf.symbols
        if s.kind == "function" and s.return_type
    }
    edges: Counter[str] = Counter()
    misses: dict[str, int] = {}
    present = set(parsed)
    present_all = set(fixture_paths())  # config files (package.json) too

    # Every class's bases across the corpus (a first pass over
    # the heritage clauses), as the ingest reads them from the inherits rows
    from server.code.domain.extract.edges_treesitter import extract_edges

    class_bases: dict[str, list[str]] = {}
    for _rel, _pf in parsed.items():
        if _pf.lang != "python" or not _pf.inherits:
            continue
        for _e in extract_edges(_pf, gate_name_table(name_table, "python")):
            if _e.edge_kind == "inherits":
                class_bases.setdefault(_e.from_qualified_name, []).append(_e.to_qualified_name)

    for rel, pf in parsed.items():
        # Import / use targets bind against the
        # corpus's own paths, as `ingest._resolve_lane_includes` binds them
        # against the namespace's live paths
        if pf.lang in ts_langs:
            specs = list(pf.includes)
            pf.includes = resolve_imports(rel, specs, present)
            # The package.json fixtures' `exports` tables
            for spec in unresolved_package_specs(rel, specs, present):
                for d in package_dirs_for(spec):
                    manifest = posixpath.join("ts/langfuse", d, "package.json")
                    if manifest not in present_all:
                        continue
                    text = open(posixpath.join(ROOT, manifest)).read()
                    hit = next(
                        (
                            c
                            for c in exports_candidates(
                                spec, posixpath.join("ts/langfuse", d), manifest_view(text)
                            )
                            if c in present
                        ),
                        None,
                    )
                    if hit and hit not in pf.includes:
                        pf.includes.append(hit)
        elif pf.lang == "rust":
            pf.includes = resolve_uses(rel, pf.includes, present)
        elif pf.lang in ("proto", "gotemplate"):
            # Import candidates settle against the corpus paths
            lane = _lane_module(pf.lang)
            pf.includes = lane.resolve_imports(rel, list(pf.includes), present)
        elif pf.lang == "config" and is_chart_manifest(rel):
            # A chart's dependencies the corpus holds
            text = pf.source.decode("utf-8", "replace")
            pf.includes = [c for c in chart_dependency_candidates(rel, text) if c in present_all]
        elif pf.lang in ("go", "java"):
            pf.includes = _code_lane_includes(rel, pf, present_all)
        elif pf.lang == "python":
            # As `ingest._resolve_py_includes` settles
            # them — the ancestors, the root, `src/` and the manifest roots
            from server.code.domain.extract.lang_python import resolve_imports as py_resolve

            files[rel]["specifiers"] = list(pf.includes)
            pf.includes = [
                p
                for p in py_resolve(rel, list(pf.includes), present_all, layout.roots_for(rel))
                if p != rel
            ]
        elif pf.lang in ("tla", "tla_config", "flatbuffers", "cmake", "doc"):
            from server.code.app.index.ingest_headers import include_candidates

            resolved: list[str] = []
            for inc in pf.includes:
                hit = next((c for c in include_candidates(rel, inc) if c in present_all), None)
                if hit and hit not in resolved and hit != rel:
                    resolved.append(hit)
            pf.includes = resolved
        gated = gate_name_table(name_table, pf.lang)
        if pf.lang == "shell":
            # As the ingest settles them — a `?cwd:` alternate
            # binds only to a file the corpus holds; a test directory's shell
            # functions bind only within it
            gated = drop_test_local_shell_defs(gated, rel)
        callee_returns: dict[str, str] = {}
        lane_context: dict[str, Any] | None = None
        if pf.lang == "go":
            # The import aliases' package directories, from
            # every fixture go.mod (the ingest's module index)
            from server.code.domain.extract.lang_go import go_module_index, go_package_context

            texts = {
                p: open(posixpath.join(ROOT, p)).read()
                for p in present_all
                if posixpath.basename(p) == "go.mod"
            }
            lane_context = {"go_packages": go_package_context(pf, go_module_index(texts))}
        if pf.lang == "python":
            # The present set, the packages' texts, the roots
            lane_context = {
                "present": set(present_all),
                "py_class_bases": class_bases,
                "py_present": set(present_all),
                "py_init_texts": {
                    p: open(posixpath.join(ROOT, p), encoding="utf-8").read()
                    for p in present_all
                    if p.endswith(".py")  # any module may re-export
                },
                "py_roots": layout.roots_for(rel),
            }
        if pf.lang in ts_langs or pf.lang in ("go", "shell"):
            # The present set gates the repo-wide rung by language
            lane_context = {**(lane_context or {}), "present": set(present_all)}
        if pf.lang in ts_langs or pf.lang in ("python", "go"):
            # Callee return types from the corpus's own symbols
            from server.code.domain.extract.edges_treesitter import callee_targets

            targets = callee_targets(pf, gated, lane_context)
            callee_returns = {
                c: returns_by_qn[q] for c, q in targets.items() if returns_by_qn.get(q)
            }
            if pf.lang == "python" and callee_returns:
                # A second round for the chains the first typed
                more = callee_targets(pf, gated, lane_context, callee_returns)
                callee_returns.update(
                    {c: returns_by_qn[q] for c, q in more.items() if returns_by_qn.get(q)}
                )
        es, ms = extract_edges_and_misses(pf, gated, None, callee_returns or None, lane_context)
        if pf.lang == "shell":
            ms = drop_program_misses(ms)
        if pf.lang in ts_langs:
            ms = drop_external_misses(pf, ms, set(pf.includes))
            # JSX + type references, import-bound
            bindings = import_bindings(pf)
            es = [*es, *reference_edges(pf, reference_sites(pf), gated, bindings)]
            if is_vue_path(rel):
                # The template's elements and handlers, as
                # `ingest._ts_reference_edges` adds them for a `.vue` path
                es = [*es, *template_edges(pf, template_sites(pf, bindings), gated, bindings)]
            # One level through the barrels the imports land on
            from server.code.domain.extract.edges_treesitter import Edge
            from server.code.domain.extract.treesitter import file_symbol_id

            for target in pf.includes:
                if target not in parsed:
                    continue
                text = parsed[target].source.decode("utf-8", "replace")
                for beyond in barrel_targets(target, text, present):
                    if beyond not in pf.includes and beyond != rel:
                        es.append(
                            Edge(
                                "includes",
                                file_symbol_id(rel),
                                file_symbol_id(beyond),
                                rel,
                                beyond,
                                rel,
                                0,
                                BARREL_CONFIDENCE,
                            )
                        )
            statements, scopes, _islands = island_records(pf)
            if statements:
                sql_gated = gate_name_table(
                    {n: name_table[n] for n in reference_names(statements) if n in name_table},
                    SQL_LANG,
                )
                # `calls`, like the SQL lane above: ingest writes the reads / writes
                # kinds; the corpus pins the extraction with `calls`
                es = [
                    *es,
                    *island_edges(pf, statements, scopes, sql_gated, scope_dialects=scope_dialects),
                ]
        if pf.lang == "prisma":
            es = [*es, *prisma_edges(pf)]
        if pf.lang in ("proto", "gotemplate"):
            # The reference sites over the lane's table
            lane = _lane_module(pf.lang)
            es = [*es, *bind_sites(pf, lane.reference_sites(pf), gated, lane.SITE_RULES)]
        if pf.lang in ("tla", "tla_config", "flatbuffers", "cmake", "doc"):
            # In-file edges plus the references bound
            # through the corpus's own name table, as `ingest._simple_lane_edges`
            es = [*es, *_simple_lane_edges(pf, name_table)]
        if pf.lang == SQL_LANG:
            statements, ju = split_file(rel, pf.source.decode("utf-8", "replace"))
            es = [*es, *sql_edges(rel, module_scope(rel), statements, pf.symbols, gated)]
            if ju.dbt:
                es.extend(jinja_edges(rel, ju, gated))
            es.extend(_column_edges(rel, statements, pf, gated))
        if pf.lang in ("python", "go", "java"):
            # The islands' table edges bind through the
            # SQL gate, as `ingest._py_island_edges` gates them — a Go caller
            # may reach a table, a Go call site may not
            statements = (
                py_sql_units(pf).statements if pf.lang == "python" else _code_units(pf).statements
            )
            sql_gated = gate_name_table(name_table, SQL_LANG)
            es = [*es, *sql_edges(rel, module_scope(rel), statements, pf.symbols, sql_gated)]
            es.extend(_column_edges(rel, statements, pf, sql_gated))
        if pf.lang in ts_langs or pf.lang in ("rust", "proto", "gotemplate", "config", "python"):
            files[rel]["includes"] = list(pf.includes)
        for e in es:
            target = kind_of.get(e.to_symbol_id, "file" if e.edge_kind == "includes" else "?")
            edges[f"{rel} | {e.edge_kind} | {target}"] += 1
        if ms:
            misses[rel] = len(ms)
    return {"files": files, "edges": dict(sorted(edges.items())), "misses": misses}


def _lane_module(lang: str) -> Any:
    """The tree-less lane modules, as `ingest._lane_module`."""
    from server.code.domain.extract import lang_gotemplate, lang_proto

    return lang_proto if lang == "proto" else lang_gotemplate


def _code_lane_includes(rel: str, pf: Any, present: set[str]) -> list[str]:
    """The include edges as `ingest._resolve_code_lane_imports` derives them: Go
    imports through the corpus's module index (every
    `go.mod` the corpus holds, replace directives included, nearest module
    wins), Java imports under the file's own source root."""
    if pf.lang == "go":
        from server.code.domain.extract.lang_go import (
            go_imports,
            go_module_index,
            go_package_file,
            go_resolve_imports_indexed,
        )

        texts = {
            p: open(posixpath.join(ROOT, p)).read()
            for p in present
            if posixpath.basename(p) == "go.mod"
        }
        index = go_module_index(texts)
        out, unresolved = go_resolve_imports_indexed(go_imports(pf), index, present)
        for _spec, pkg_dir in unresolved:  # The directory listing fallback
            if not any(p.rsplit("/", 1)[0] == pkg_dir for p in out):
                file = go_package_file(sorted(present), pkg_dir)
                if file:
                    out.append(file)
        return [p for p in out if p != rel]
    from server.code.domain.extract.lang_java import (
        java_imports,
        java_package,
        java_resolve_imports,
        java_source_root,
    )

    root = java_source_root(rel, java_package(pf))
    return [p for p in java_resolve_imports(java_imports(pf), [root], present) if p != rel]


def _code_units(pf: Any) -> Any:
    """The SQL islands of a Go or Java file."""
    if pf.lang == "go":
        from server.code.domain.extract.lang_go import go_sql_units

        return go_sql_units(pf)
    from server.code.domain.extract.lang_java import java_sql_units

    return java_sql_units(pf)


def _column_edges(rel: str, statements: list[Any], pf: Any, gated: dict[str, Any]) -> list[Any]:
    """The column edges as `ingest._ColumnAsk` derives them: the file's own
    columns plus the corpus-confirmed hypotheses, then statement → column."""
    from server.code.domain.extract.lang_python import module_scope
    from server.code.domain.extract.lang_sql_columns import (
        column_edges,
        column_hypotheses,
        file_columns,
        known_from_name_table,
        merge_known,
    )

    known = file_columns(statements)
    hyps = column_hypotheses(statements, known)
    known = merge_known(known, known_from_name_table(gated, hyps))
    return column_edges(rel, module_scope(rel), statements, known, pf.symbols, gated)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="regenerate expected.json")
    args = ap.parse_args(argv)
    summary = summarize()
    text = json.dumps(summary, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    if args.write:
        with open(EXPECTED, "w") as fh:
            fh.write(text)
        print(
            f"wrote {EXPECTED}: {len(summary['files'])} files, {sum(summary['edges'].values())} edges"
        )
        return 0
    sys.stdout.write(text)
    return 0


def _simple_lane_edges(pf: Any, name_table: dict[str, list[tuple[str, str, str]]]) -> list[Any]:
    """The lanes' edge derivation with the corpus name table standing in
    for `repo.name_candidates`."""
    if pf.lang == "tla":
        from server.code.domain.extract.lang_tla import tla_external_edges, tla_local_edges

        local, external = tla_local_edges(pf)
        return [*local, *tla_external_edges(pf, external, name_table)]
    if pf.lang == "tla_config":
        from server.code.domain.extract.lang_tla import tlc_edges

        return tlc_edges(pf, name_table)
    if pf.lang == "flatbuffers":
        from server.code.domain.extract.lang_flatbuffers import fbs_external_edges, fbs_local_edges

        local, external = fbs_local_edges(pf)
        return [*local, *fbs_external_edges(pf, external, name_table)]
    if pf.lang == "cmake":
        from server.code.domain.extract.lang_cmake import cmake_external_edges, cmake_local_edges

        local, external = cmake_local_edges(pf)
        return [*local, *cmake_external_edges(pf, external, name_table)]
    if pf.lang == "doc":
        from server.code.domain.extract.lang_markdown import (
            doc_reference_edges,
            doc_reference_sites,
        )

        return doc_reference_edges(pf, doc_reference_sites(pf), name_table)
    return []


if __name__ == "__main__":
    raise SystemExit(main())
