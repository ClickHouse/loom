"""Extractor stamp: identity of the parser tier's producer, the per-stage
stamps, and the paced re-chunk that converges a namespace
onto a new stamp with exactly the stages that moved."""

from __future__ import annotations

import asyncio
import json
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from server.code.domain.extract import extractor_stamp as es
from server.code.domain.extract.extractor_stamp import (
    EXTRACTOR_MODULES,
    STAGE_INPUTS,
    STAGE_MODULES,
    STAGES,
    compute_stage_stamps,
    compute_stamp,
    moved_stages,
    stage_modules,
)
from server.settings import settings

# Modules under server/code/domain/extract/ that do NOT decide what a row looks like
# — orchestration, I/O, read-side helpers, workers' inputs. A new module must
# land here (with its reason) or in one stage of STAGE_MODULES; nowhere is
# not an option, because "nowhere" is how a row-shaping change escapes the
# stamp and leaves rows that nothing invalidates.
NON_EXTRACTOR_MODULES: dict[str, str] = {
    "py_notebooks.py": (
        "a notebook's code cells as one Python text with cell markers — "
        "decides how an .ipynb is READ before the parse, like lang_detect decides which files "
        "index; a namespace's notebooks gain their symbols by a targeted backfill over the "
        ".ipynb paths or the heal after the next cut (both re-read the source)"
    ),
    "go_receivers.py": (
        "Go receiver types for the call ladder — a lane module outside the "
        "stamp like lang_go.py: a Go namespace gains the edges by a "
        "targeted backfill over its Go tree, never by a heal"
    ),
    "go_implementors.py": (
        "structural interface satisfaction for Go — a lane module outside the "
        "stamp like lang_go.py; a Go namespace gains the inherits edges by a targeted backfill"
    ),
    "go_grpc.py": (
        "a Go handler implements the proto lane's service — a lane module "
        "outside the stamp like lang_go.py; a Go namespace gains the edges by a targeted backfill"
    ),
    "go_directives.py": (
        "go:embed reads, go:generate / struct-tag / cobra / kubebuilder facets — "
        "a lane module outside the stamp like lang_go.py; rows arrive by a targeted backfill"
    ),
    "lang_text.py": "text-fallback language table, chunk-only; no extractor stage reads it",
    "lang_code_islands.py": (
        "the island contract with the grammar as data — Go and Java client "
        "calls, annotations, format templates and literals; dispatched from ingest.py"
    ),
    "lang_go.py": (
        "Go parser: functions, methods, type specs, import paths and the "
        "SQL islands; applied by a targeted backfill over a namespace's Go tree, never a heal"
    ),
    "lang_vue.py": (
        "Vue single-file components: the script block parsed offset-"
        "preservingly through lang_typescript, template sites and their binder; applied by a "
        "targeted backfill over a namespace's .vue tree, never a heal"
    ),
    "lang_sfc.py": (
        "Svelte and Astro components: the Vue rule for two more "
        "markup languages — script laid through lang_typescript, markup sites bound by "
        "lang_vue's binder; applied by a targeted backfill over the .svelte / .astro tree"
    ),
    "lang_angular.py": (
        "Angular components and templates: selector metadata stamped on the "
        "class after the TypeScript parse, external templates as their module's unit, selector "
        "and handler sites bound in the reference pass; applied by a targeted backfill"
    ),
    "lane_units.py": (
        "what the tree-less lanes share — span chunks in the "
        "chunker's shape and the reference-site binder; dispatched from ingest.py"
    ),
    "lang_proto.py": (
        "protobuf scanner: messages, enums, services, rpcs, type sites and "
        "import candidates; applied by a targeted backfill over a namespace's .proto files"
    ),
    "lang_jsonnet.py": (
        "jsonnet bindings, members and imports — a lane module outside the "
        "stamp like lang_proto.py; rows arrive by a targeted backfill"
    ),
    "lang_k8s.py": (
        "plain Kubernetes manifests (documents, references, facets) — a lane "
        "module outside the stamp like lang_proto.py; rows arrive by a targeted backfill"
    ),
    "lang_hcl.py": (
        "Terraform / terragrunt lane (token scanner) — a lane module outside the "
        "stamp like lang_proto.py; a namespace gains its rows by a targeted backfill"
    ),
    "lang_gotemplate.py": (
        "Go template / Helm scanner: defines, manifest documents, include "
        "sites and chart dependencies; applied by a targeted backfill, never a heal"
    ),
    "lang_java.py": (
        "Java parser: types, methods, constructors, imports and the SQL "
        "islands; applied by a targeted backfill, never a heal"
    ),
    "lang_sql_columns.py": (
        "column-level lineage — column symbols, column_read / column_write "
        "facets and statement → column edges behind code_sql_column_lineage; applied by a "
        "targeted backfill over the SQL and Python trees, never by a stamp heal"
    ),
    "lang_py_islands.py": (
        "the SQL a Python file runs as statement records, facets and edges; "
        "off by default, applied by `loom code backfill --paths-file`, never by a stamp heal"
    ),
    "__init__.py": "package",
    "lang_tla.py": "TLA+ specs + TLC configs as symbols / chunks / edges / facets — a lane module, outside the stamp like lang_rust",
    "lang_flatbuffers.py": "FlatBuffers schemas — a lane module",
    "lang_cmake.py": "CMake targets, builds / tests facets, link graph — a lane module",
    "lang_markdown.py": "Markdown design docs by heading — a lane module",
    "extractor_stamp.py": "the stamp itself",
    "bounded_parse.py": "the parse limits — the same tree within them, no tree past them",
    "parse_child.py": "the parse limits' child process — no tree of its own",
    "lang_reference.py": (
        "reference fixtures: one unembedded chunk per file, written only "
        "for paths no stage's parser touches; a stamp heal walks LIVE rows and cannot add "
        "files, so a rule change here is applied by `loom code backfill --paths-file` over "
        "the reference tree — never by a namespace-wide re-chunk"
    ),
    "lang_bats.py": (
        "bats test files: a length-preserving rewrite of `@test` headers fed to "
        "the stamped bash parser, symbols renamed, `load` targets as includes; files no parser "
        "touched before have no rows to heal — a namespace gains them by a targeted backfill"
    ),
    "lang_hosts.py": (
        "host islands: shell inside CI YAML, Dockerfiles, Makefiles and "
        "Jenkinsfiles read through the stamped bash parser for facets, includes and job / "
        "target / stage symbols; chunks stay the line windows, so nothing a heal walks "
        "changes shape — host paths gain their rows by a targeted backfill"
    ),
    "lang_sql_dialect.py": (
        "the dialect a SQL file is written in, from its text: one file-level "
        "facet beside the statement records; applied by `loom code backfill --paths-file`"
    ),
    "lang_sql_jinja.py": (
        "Jinja-templated SQL: line-preserving neutralization before the split, "
        "dbt model / macro symbols, ref / source / config facets and model → model edges — "
        "rows beside the statement records, applied by `loom code backfill --paths-file`"
    ),
    "lang_sql_objects.py": (
        "SQL created objects and table references: table / view / … symbols, "
        "creates / table_read / table_write facets and statement → object edges beside the "
        "statement records; applied by `loom code backfill --paths-file` over the SQL tree"
    ),
    "lang_sql_directives.py": (
        "SQL migration / query tool directives: tool, version and direction "
        "facets, changeset and named-query symbols, psql includes — rows beside the statement "
        "records of lang_sql.py, applied by `loom code backfill --paths-file` over the SQL tree"
    ),
    "lang_detect.py": (
        "which files are shell: Linguist names, ShellCheck's directive/shebang "
        "order, the extensionless test-tree heuristic — WHICH files index, like denylist.py, "
        "not a row's shape; new files arrive by a targeted backfill, never a heal"
    ),
    "lang_shell_islands.py": (
        "shell SQL islands + leftover coalescing: post-processes the AST "
        "chunker's output for `shell` files from ingest.py; a stamp heal cannot add the "
        "island symbols the old rows never had, so the shell tree is re-chunked by "
        "`loom code backfill --paths-file`, never by a namespace-wide heal"
    ),
    "lang_typescript.py": (
        "TypeScript / JavaScript parser: module-scoped symbols, import "
        "specifiers resolved against the namespace's live paths, extends / implements; a "
        "file no parser touched before has no rows to heal, so a fix here is applied by "
        "`loom code backfill --paths-file` over a namespace's TypeScript tree"
    ),
    "lang_typescript_refs.py": (
        "TypeScript / JavaScript type, value and member reference edges, the "
        "TypeScript lane's own; applied by the same targeted backfill as lang_typescript.py"
    ),
    "lang_typescript_nodes.py": (
        "TypeScript / JavaScript syntax-tree helpers the TypeScript parser walks "
        "with; applied by the same targeted backfill as lang_typescript.py"
    ),
    "lang_typescript_imports.py": (
        "TypeScript / JavaScript import bindings and candidate paths (aliases, "
        "workspace packages, package exports, barrels), the TypeScript lane's own; applied by "
        "the same targeted backfill as lang_typescript.py"
    ),
    "lang_rust.py": (
        "Rust parser: crate-path symbols, impl blocks as skeletons, use / mod "
        "targets resolved against live paths; applied by a targeted backfill, never a heal"
    ),
    "error_codes.py": (
        "ClickHouse error codes as symbols: one file per repository, "
        "src/Common/ErrorCodes.cpp; applied by a targeted `loom code backfill --paths-file` "
        "over that file, never a fleet heal"
    ),
    "lang_prisma.py": (
        "Prisma schema: model / enum symbols, creates facets, relation edges "
        "and one chunk per block; applied by a targeted backfill over the schema"
    ),
    "lang_ts_islands.py": (
        "SQL inside TypeScript template literals: the shell-island contract's "
        "TypeScript row — statement records under the enclosing function, facets, edges to the "
        "tables the migrations create; dispatched from ingest.py"
    ),
    "lang_sql_model.py": (
        "SQL lane record types and facet kinds, read by lang_sql.py and its "
        "split / facet modules; applied by the same targeted backfill as lang_sql.py"
    ),
    "lang_sql_split.py": (
        "SQL statement splitting (delimiter modes, comments, hints, kinds and "
        "spans) for lang_sql.py; applied by the same targeted backfill as lang_sql.py"
    ),
    "lang_sql_facets.py": (
        "SQL statement facets (functions, settings, engines, system tables, "
        "formats, types) for lang_sql.py; applied by the same targeted backfill as lang_sql.py"
    ),
    "lang_sql.py": (
        "SQL statement records: parser, statement-packed chunks, context "
        "prefixes and facets for `.sql` files only, dispatched from ingest.py; a stamp heal "
        "cannot add the symbols the old rows never had, so the SQL tree is re-chunked by "
        "`loom code backfill --paths-file`, never by a namespace-wide heal"
    ),
    "env_reads.py": (
        "environment-variable reads as `env_var` facets for TypeScript, "
        "JavaScript and Python files, dispatched from ingest.py; files that already carry "
        "rows gain them through `loom code backfill --paths-file`"
    ),
    "py_routes.py": (
        "HTTP route, CLI command, decorator and async-modifier facets for Python files, "
        "dispatched from ingest.py beside env_reads.py; a stamp heal cannot "
        "add facets to rows that never carried them, so Python files gain them by "
        "`loom code backfill --paths-file`"
    ),
    "text_lines.py": (
        "the line of an offset, for the route, environment, template and dbt lanes outside the stamp"
    ),
    "ts_routes.py": (
        "HTTP route facets for TypeScript/JavaScript files: file-system, "
        "decorator and registration routers become `http_route` facet rows, dispatched from "
        "ingest.py; a stamp heal cannot add facets to rows that never carried them, so route "
        "files are re-chunked by `loom code backfill --paths-file`"
    ),
    "name_scope.py": (
        "which repo-wide candidates a call site may bind to, by language class: "
        "it narrows the table `ingest._derive_edges` hands the stamped resolver; rows "
        "derived under the wider table are repaired by `loom code backfill --paths-file` over "
        "the script paths, never by an edge-stage heal of every C++ file"
    ),
}


def test_every_code_index_module_is_in_exactly_one_stage_or_declared_not_an_extractor() -> None:
    here = pathlib.Path(es.__file__).resolve().parent
    on_disk = {p.name for p in here.glob("*.py")}
    staged: dict[str, str] = {}
    for stage, mods in STAGE_MODULES.items():
        for m in mods:
            assert m not in staged, f"{m} is in both {staged[m]} and {stage}"
            staged[m] = stage
    assert set(staged) & set(NON_EXTRACTOR_MODULES) == set(), "a module is not both"
    unplaced = on_disk - set(staged) - set(NON_EXTRACTOR_MODULES)
    assert not unplaced, f"place these in a stage or in NON_EXTRACTOR_MODULES: {sorted(unplaced)}"
    gone = (set(staged) | set(NON_EXTRACTOR_MODULES)) - on_disk
    assert not gone, f"listed but not on disk: {sorted(gone)}"


def test_stage_partition_is_pinned() -> None:
    assert STAGES == ("chunk", "symbol", "edge", "guard")
    assert STAGE_MODULES == {
        "chunk": ("chunker.py", "context_text.py", "tokenize.py", "callee_outline.py"),
        "symbol": ("treesitter.py", "lang_python.py", "py_members.py", "lang_bash.py"),
        "edge": (
            "edges_treesitter.py",
            "crossref.py",
            "registration.py",
            "ts_receivers.py",
            "py_receivers.py",
            "py_library_methods.py",
            "ts_config.py",
            "ts_reexports.py",
            "cpp_receivers.py",
            "ts_workspace.py",
            "py_layouts.py",
        ),
        "guard": ("call_guards.py",),
    }
    assert STAGE_INPUTS == {
        "chunk": ("symbol", "edge"),
        "symbol": (),
        "edge": ("symbol",),
        "guard": ("edge",),
    }
    # the combined stamp hashes every extractor module, including the guard
    # stage's, the registration module (an edge-stage module: its rows are
    # edges, so it moves the edge, guard and chunk stamps, never the symbol
    # one) and the Python library-method set
    assert EXTRACTOR_MODULES == (
        "call_guards.py",
        "callee_outline.py",
        "chunker.py",
        "context_text.py",
        "cpp_receivers.py",
        "crossref.py",
        "edges_treesitter.py",
        "lang_bash.py",
        "lang_python.py",
        "py_layouts.py",
        "py_library_methods.py",
        "py_members.py",
        "py_receivers.py",
        "registration.py",
        "tokenize.py",
        "treesitter.py",
        "ts_config.py",
        "ts_receivers.py",
        "ts_reexports.py",
        "ts_workspace.py",
    )
    assert stage_modules("symbol") == (
        "lang_bash.py",
        "lang_python.py",
        "py_members.py",
        "treesitter.py",  # members
    )
    assert stage_modules("edge") == (
        "cpp_receivers.py",
        "crossref.py",
        "edges_treesitter.py",
        "lang_bash.py",
        "lang_python.py",
        "py_layouts.py",  # The manifest roots an import resolves under
        "py_library_methods.py",
        "py_members.py",
        "py_receivers.py",
        "registration.py",
        "treesitter.py",
        "ts_config.py",
        "ts_receivers.py",
        "ts_reexports.py",
        "ts_workspace.py",
    )
    assert stage_modules("guard") == (
        "call_guards.py",
        "cpp_receivers.py",
        "crossref.py",
        "edges_treesitter.py",
        "lang_bash.py",
        "lang_python.py",
        "py_layouts.py",
        "py_library_methods.py",
        "py_members.py",
        "py_receivers.py",
        "registration.py",
        "treesitter.py",
        "ts_config.py",
        "ts_receivers.py",
        "ts_reexports.py",
        "ts_workspace.py",
    ), "the guard stage reads the parse and the resolved edges"
    assert set(stage_modules("chunk")) == set(EXTRACTOR_MODULES) - {"call_guards.py"}, (
        "the chunk stage reads every other stage but the guard stage, which nothing reads"
    )


def test_stamp_changes_with_module_code_and_grammar_versions(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.py").write_bytes(b"def f(): pass\n")
    (tmp_path / "b.py").write_bytes(b"X = 1\n")
    base = compute_stamp(tmp_path, ("a.py", "b.py"), {"tree-sitter-cpp": "0.23.4"})
    assert len(base) == 16 and int(base, 16) >= 0
    assert compute_stamp(tmp_path, ("b.py", "a.py"), {"tree-sitter-cpp": "0.23.4"}) == base, (
        "order-free"
    )
    (tmp_path / "b.py").write_bytes(b"X = 2\n")
    assert compute_stamp(tmp_path, ("a.py", "b.py"), {"tree-sitter-cpp": "0.23.4"}) != base, (
        "a code change moves it"
    )
    (tmp_path / "b.py").write_bytes(b"X = 1\n")
    assert compute_stamp(tmp_path, ("a.py", "b.py"), {"tree-sitter-cpp": "0.24.0"}) != base, (
        "a grammar upgrade moves it"
    )
    assert compute_stamp(tmp_path, ("a.py", "b.py", "missing.py"), {}) != compute_stamp(
        tmp_path, ("a.py", "b.py"), {}
    )


def test_prose_does_not_move_the_stamp(tmp_path: pathlib.Path) -> None:
    (tmp_path / "a.py").write_text('"""Module doc."""\n\ndef f(x):\n    """Doc."""\n    return x\n')
    base = compute_stamp(tmp_path, ("a.py",), {})
    (tmp_path / "a.py").write_text(
        '"""Reworded."""\n\n\n# a comment\ndef f(x):  # trailing\n    return x\n'
    )
    assert compute_stamp(tmp_path, ("a.py",), {}) == base, "comments and docstrings are prose"
    (tmp_path / "a.py").write_text('def f(x):\n    return "Doc."\n')
    assert compute_stamp(tmp_path, ("a.py",), {}) != base, "a returned string is code"


def _write_modules(d: pathlib.Path) -> None:
    for m in EXTRACTOR_MODULES:
        (d / m).write_bytes(f"NAME = {m!r}\n".encode())


def test_stage_stamps_move_with_their_inputs(tmp_path: pathlib.Path) -> None:
    _write_modules(tmp_path)
    g = {"tree-sitter-cpp": "0.23.4"}
    base = compute_stage_stamps(tmp_path, g)
    assert set(base) == set(STAGES) and len(set(base.values())) == 4

    (tmp_path / "chunker.py").write_bytes(b"NAME = 'chunker v2'\n")
    assert moved_stages(base, compute_stage_stamps(tmp_path, g)) == {"chunk"}, "chunker: chunk only"
    _write_modules(tmp_path)

    (tmp_path / "edges_treesitter.py").write_bytes(b"NAME = 'edges v2'\n")
    assert moved_stages(base, compute_stage_stamps(tmp_path, g)) == {"chunk", "edge", "guard"}, (
        "edge modules feed the callee outline and the guard records; symbols do not"
    )
    _write_modules(tmp_path)

    (tmp_path / "call_guards.py").write_bytes(b"NAME = 'guards v2'\n")
    assert moved_stages(base, compute_stage_stamps(tmp_path, g)) == {"guard"}, (
        "a guard-extractor change re-derives guard rows only"
    )
    _write_modules(tmp_path)

    (tmp_path / "treesitter.py").write_bytes(b"NAME = 'parser v2'\n")
    assert moved_stages(base, compute_stage_stamps(tmp_path, g)) == set(STAGES), (
        "the parse feeds every stage"
    )
    _write_modules(tmp_path)

    assert moved_stages(base, compute_stage_stamps(tmp_path, {"tree-sitter-cpp": "0.24.0"})) == set(
        STAGES
    ), "a grammar upgrade is a parse change"
    assert moved_stages(base, compute_stage_stamps(tmp_path, g)) == frozenset()
    assert moved_stages({}, base) == set(STAGES), (
        "no record = moved; the worker decides what empty means"
    )


def test_stage_stamps_and_combined_stamp_agree_on_the_running_tree() -> None:
    stages = es.stage_stamps()
    assert set(stages) == set(STAGES)
    assert stages["chunk"] != es.extractor_stamp(), (
        "the chunk stage hashes every module but the guard stage's; the combined stamp has it"
    )
    assert (
        es.compute_stamp(es._HERE, EXTRACTOR_MODULES, es.grammar_versions()) == es.extractor_stamp()
    )


# ---------------------------------------------------------------------------
# the worker's convergence
# ---------------------------------------------------------------------------

CUR = {"chunk": "c1", "symbol": "s1", "edge": "e1", "guard": "g1"}
# the three keys a namespace recorded before the guard stage existed
PRE_GUARD = {"chunk": "c1", "symbol": "s1", "edge": "e1"}


class _Repo:
    def __init__(self, paths: list[str], state: dict[str, dict[str, str]] | None = None) -> None:
        self.paths = paths
        self.state = state or {}
        self.writes: list[tuple[str, str, dict[str, Any]]] = []

    async def all_state(self, org, ns):
        return self.state

    async def set_state(self, org, ns, key, value, detail=""):
        self.state[key] = {"value": value, "detail": detail}
        self.writes.append((key, value, json.loads(detail or "{}")))

    async def live_file_hashes(self, org, ns):
        return {p: "h" for p in self.paths}

    def stamp_writes(self) -> list[tuple[str, str, dict[str, Any]]]:
        return [w for w in self.writes if w[0] == "extractor_stamp"]

    def stage_values(self) -> dict[str, str]:
        return {s: (self.state.get(f"extractor_stamp_{s}") or {}).get("value", "") for s in STAGES}


def _cfg():
    return SimpleNamespace(
        org="o", namespace="n", git_dir="/nowhere", embed_reuse_from="", label="o/n"
    )


def _state(value: str, detail: dict[str, Any] | None = None, stages: dict[str, str] | None = None):
    st = {"extractor_stamp": {"value": value, "detail": json.dumps(detail or {})}}
    for s, v in (stages or {}).items():
        st[f"extractor_stamp_{s}"] = {"value": v, "detail": "{}"}
    return st


def _run(
    repo,
    *,
    stamp: str,
    stages: dict[str, str] | None = None,
    per_pass: int = 2,
    passes: int = 1,
) -> tuple[list[tuple[list[str], frozenset[str] | None]], Any]:
    from server.code.app.index import ingest_pass as w
    from server.code.app.index import pass_budget as ingest_budget

    healed: list[tuple[list[str], frozenset[str] | None]] = []

    async def fake_heal(repo_, cfg, head, paths, *, embedder, stages=None, live_hashes=None, **_kw):
        healed.append((list(paths), stages))
        assert live_hashes is not None and set(paths) <= set(live_hashes), (
            "the heal gets the live hashes"
        )
        return SimpleNamespace(
            files=len(paths),
            chunks_new=0,
            chunks_moved=0,
            symbols_moved=0,
            errors=0,
            identical=1,
            scoped=len(paths),
        )

    result = ingest_budget.CodeIngestResult()

    async def go():
        with (
            patch("server.code.domain.extract.extractor_stamp.extractor_stamp", return_value=stamp),
            patch(
                "server.code.domain.extract.extractor_stamp.stage_stamps",
                return_value=dict(stages or CUR),
            ),
            patch("server.code.app.index.reconcile.heal", new=fake_heal),
            patch(
                "server.embedding.embedder.get_embedder",
                return_value=SimpleNamespace(model_name="fake"),
            ),
            patch.object(settings, "code_extractor_rechunk_per_pass", per_pass),
        ):
            for _ in range(passes):
                await w._maybe_rechunk(repo, _cfg(), "head", result)

    asyncio.run(go())
    return healed, result


def test_first_observation_is_recorded_not_acted_on() -> None:
    repo = _Repo(["src/a.cpp", "src/b.cpp"])
    batches, result = _run(repo, stamp="s1")
    assert batches == [], "we cannot know what existing rows were produced by"
    assert (
        repo.writes[0][:2] == ("extractor_stamp", "s1") and repo.writes[0][2]["first_observation"]
    )
    assert repo.stage_values() == CUR, "the stage keys are recorded with it"
    assert result.rechunked == 0


def test_converged_namespace_does_nothing() -> None:
    repo = _Repo(["src/a.cpp"], _state("s1", stages=CUR))
    batches, _ = _run(repo, stamp="s1")
    assert batches == [] and repo.writes == []


def test_a_namespace_at_the_combined_stamp_without_stage_keys_heals_every_stage() -> None:
    """No stage key can say which stage is current, so every stage heals and
    completion records the keys; the next pass is silent."""
    repo = _Repo(["src/a.cpp", "src/b.cpp"], _state("s1"))
    batches, _ = _run(repo, stamp="s1")
    assert batches == [(["src/a.cpp", "src/b.cpp"], frozenset(STAGES))]
    assert repo.stage_values() == CUR
    repo.writes.clear()
    batches, _ = _run(repo, stamp="s1")
    assert batches == [] and repo.writes == []


def test_a_migration_without_stage_keys_continues_at_its_cursor_with_every_stage() -> None:
    """A re-chunk mid-way with no stage keys is not restarted and not
    narrowed: nothing recorded says which stage moved."""
    repo = _Repo(
        ["src/a.cpp", "src/b.cpp", "src/c.cpp", "src/d.cpp"],
        _state("s0", {"target": "s1", "cursor": 2, "total": 4}),
    )
    batches, _ = _run(repo, stamp="s1", per_pass=2)
    assert batches == [(["src/c.cpp", "src/d.cpp"], frozenset(STAGES))]
    assert repo.state["extractor_stamp"]["value"] == "s1"
    assert repo.stage_values() == CUR, "completion records the stage keys"


def test_no_stage_keys_at_an_older_combined_stamp_heals_every_stage() -> None:
    repo = _Repo(["src/a.cpp"], _state("s0"))
    batches, _ = _run(repo, stamp="s1")
    assert batches == [(["src/a.cpp"], frozenset(STAGES))]


def test_a_chunker_only_move_heals_the_chunk_stage_only() -> None:
    paths = ["src/e.cpp", "src/a.cpp", "src/c.cpp", "src/b.cpp", "src/d.cpp"]
    repo = _Repo(
        paths, _state("s1", stages={"chunk": "c0", "symbol": "s1", "edge": "e1", "guard": "g1"})
    )
    batches, result = _run(repo, stamp="s2", per_pass=2, passes=3)
    assert [b[0] for b in batches] == [
        ["src/a.cpp", "src/b.cpp"],
        ["src/c.cpp", "src/d.cpp"],
        ["src/e.cpp"],
    ], "sorted paths, stable cursor across passes"
    assert all(b[1] == frozenset({"chunk"}) for b in batches), (
        "the symbol and edge blocks are skipped"
    )
    assert result.rechunked == 5 and result.rechunk_identical == 3
    # progress rows keep the OLD value with target + cursor + stages; the last write flips the value
    assert [w[1] for w in repo.stamp_writes()] == ["s1", "s1", "s2"]
    first = repo.stamp_writes()[0][2]
    assert first == {
        "at": first["at"],
        "target": "s2",
        "cursor": 2,
        "total": 5,
        "stages": ["chunk"],
        "identical": 1,
    }
    last = repo.stamp_writes()[-1][2]
    assert last["completed"] is True and last["from"] == "s1" and last["stages"] == ["chunk"]
    assert last["identical"] == 3
    assert repo.stage_values() == CUR
    stage_writes = [w for w in repo.writes if w[0].startswith("extractor_stamp_")]
    assert stage_writes and all(w[2].get("completed") for w in stage_writes), (
        "stage keys are rewritten at completion, never while the cursor is moving"
    )


def test_an_edge_move_heals_chunk_edge_and_guard_and_a_parse_move_heals_everything() -> None:
    repo = _Repo(
        ["src/a.cpp"],
        _state("s1", stages={"chunk": "c0", "symbol": "s1", "edge": "e0", "guard": "g0"}),
    )
    batches, _ = _run(repo, stamp="s2")
    assert batches[0][1] == frozenset({"chunk", "edge", "guard"})
    repo = _Repo(
        ["src/a.cpp"],
        _state("s1", stages={"chunk": "c0", "symbol": "s0", "edge": "e0", "guard": "g0"}),
    )
    batches, _ = _run(repo, stamp="s2")
    assert batches[0][1] == frozenset(STAGES)


def test_a_namespace_recorded_before_the_guard_stage_heals_the_guard_stage_only() -> None:
    """The nine code namespaces carry chunk/symbol/edge keys
    and no guard key. Adding the stage moves the combined stamp; the plan
    must heal `guard` alone — not walk every stage."""
    repo = _Repo(["src/a.cpp", "src/b.cpp"], _state("s1", stages=PRE_GUARD))
    batches, result = _run(repo, stamp="s2")
    assert batches == [(["src/a.cpp", "src/b.cpp"], frozenset({"guard"}))]
    assert result.rechunked == 2
    assert repo.state["extractor_stamp"]["value"] == "s2"
    assert repo.stage_values() == CUR, "completion records every key, the guard key included"


def test_the_guard_stage_joins_an_in_flight_heal_as_a_union() -> None:
    """A chunk+edge walk is mid-way when the guard stage lands:
    the cursor restarts against the new target with chunk+edge+guard — one
    walk that fills the guard table, not a second one after it."""
    repo = _Repo(
        ["src/a.cpp", "src/b.cpp", "src/c.cpp"],
        _state(
            "s1",
            {"target": "s2", "cursor": 2, "total": 3, "stages": ["chunk", "edge"]},
            stages={"chunk": "c0", "symbol": "s1", "edge": "e0"},
        ),
    )
    batches, _ = _run(repo, stamp="s3", per_pass=2)
    assert batches == [(["src/a.cpp", "src/b.cpp"], frozenset({"chunk", "edge", "guard"}))]
    last = repo.stamp_writes()[-1][2]
    assert (
        last["target"] == "s3"
        and last["cursor"] == 2
        and last["stages"] == ["chunk", "edge", "guard"]
    )


def test_combined_moved_but_no_stage_did_heals_everything() -> None:
    """Defensive: a module in the combined set but in no stage would show as
    this — heal everything rather than nothing."""
    repo = _Repo(["src/a.cpp"], _state("s1", stages=CUR))
    batches, _ = _run(repo, stamp="s2")
    assert batches[0][1] == frozenset(STAGES)


def test_stamp_moving_again_mid_migration_restarts_the_cursor_with_the_union_of_moves() -> None:
    # recorded (pre-migration) stages: chunk c0, symbol s1, edge e1; the first
    # move (c0 -> c1) is chunk-only and 2 files in; then the edge modules
    # move too (e1 -> e2): the scope is re-derived against the RECORDED stamps
    repo = _Repo(
        ["src/a.cpp", "src/b.cpp", "src/c.cpp"],
        _state(
            "s1",
            {"target": "s2", "cursor": 2, "total": 3, "stages": ["chunk"]},
            stages={"chunk": "c0", "symbol": "s1", "edge": "e1", "guard": "g1"},
        ),
    )
    batches, _ = _run(
        repo,
        stamp="s3",
        stages={"chunk": "c1", "symbol": "s1", "edge": "e2", "guard": "g1"},
        per_pass=2,
    )
    assert batches == [(["src/a.cpp", "src/b.cpp"], frozenset({"chunk", "edge"}))], (
        "s3 is a new target: start over with chunk+edge, do not resume s2's chunk-only cursor"
    )
    last = repo.stamp_writes()[-1][2]
    assert last["target"] == "s3" and last["cursor"] == 2 and last["stages"] == ["chunk", "edge"]


def test_stage_plan_is_pure_and_names_every_action() -> None:
    from server.code.app.index.ingest_pass import _stage_plan

    cur = "s2"
    assert _stage_plan("", {}, {}, cur, CUR) == ("first", frozenset(), {})
    assert _stage_plan("s2", {}, CUR, cur, CUR) == ("converged", frozenset(), {})
    assert _stage_plan("s2", {}, {}, cur, CUR)[:2] == ("heal", frozenset(STAGES)), (
        "no stage keys at the combined stamp: every stage"
    )
    a, st, d = _stage_plan("s1", {}, {}, cur, CUR)
    assert (a, st, d) == (
        "heal",
        frozenset(STAGES),
        {"target": "s2", "cursor": 0, "stages": ["chunk", "edge", "guard", "symbol"]},
    ), "no stage keys at an older stamp: every stage"
    a, st, d = _stage_plan(
        "s1", {}, {"chunk": "c0", "symbol": "s1", "edge": "e1", "guard": "g1"}, cur, CUR
    )
    assert (a, st, d["stages"], d["cursor"]) == ("heal", frozenset({"chunk"}), ["chunk"], 0)
    a, st, d = _stage_plan(
        "s1",
        {"target": "s2", "cursor": 4, "stages": ["chunk"]},
        {"chunk": "c0", "symbol": "s1", "edge": "e1", "guard": "g1"},
        cur,
        CUR,
    )
    assert (a, st, d["cursor"]) == ("heal", frozenset({"chunk"}), 4), (
        "target is the running stamp: continue"
    )
    a, st, d = _stage_plan(
        "s1",
        {"target": "s9", "cursor": 4, "stages": ["chunk"]},
        {"chunk": "c0", "symbol": "s1", "edge": "e0", "guard": "g1"},
        cur,
        CUR,
    )
    assert (a, st, d["cursor"], d["stages"]) == (
        "heal",
        frozenset({"chunk", "edge"}),
        0,
        ["chunk", "edge"],
    ), "another target: restart with the union of moves against the recorded stamps"
    a, st, d = _stage_plan("s1", {}, PRE_GUARD, cur, CUR)
    assert (a, st, d["stages"]) == ("heal", frozenset({"guard"}), ["guard"]), (
        "three keys recorded, no guard key: the missing stage alone"
    )
    a, st, _ = _stage_plan("s2", {}, PRE_GUARD, cur, CUR)
    assert (a, st) == ("heal", frozenset({"guard"})), (
        "even at the combined stamp a missing stage key is healed"
    )
    a, st, _ = _stage_plan("s1", {"target": "s2", "cursor": 1, "stages": ["bogus"]}, CUR, cur, CUR)
    assert (a, st) == ("heal", frozenset(STAGES)), "an unreadable scope heals everything"
    a, st, _ = _stage_plan("s1", {}, CUR, cur, CUR)
    assert (a, st) == ("heal", frozenset(STAGES)), "combined moved, no stage did: everything"


def test_disabled_is_a_no_op() -> None:
    repo = _Repo(["src/a.cpp"], _state("s1", stages=CUR))
    with patch.object(settings, "code_extractor_rechunk_enabled", False):
        batches, _ = _run(repo, stamp="s2")
    assert batches == [] and repo.writes == []


def test_only_the_stage_grammars_are_stamped() -> None:
    """A lane grammar (typescript, rust, go, java, css) is
    outside the stamp like its module; the host venv carrying one the image
    lacks must not move every namespace's stamp."""
    from server.code.domain.extract import extractor_stamp as es

    assert es.STAMPED_GRAMMARS == {
        "tree-sitter",
        "tree-sitter-cpp",
        "tree-sitter-python",
        "tree-sitter-bash",
    }
    versions = es.grammar_versions()
    assert set(versions) <= es.STAMPED_GRAMMARS
    assert {"tree-sitter", "tree-sitter-cpp", "tree-sitter-python", "tree-sitter-bash"} <= set(
        versions
    )


def test_an_import_path_does_not_move_the_stamp_and_an_imported_name_does(
    tmp_path: pathlib.Path,
) -> None:
    def stamp(src: str) -> str:
        (tmp_path / "a.py").write_text(src)
        return compute_stamp(tmp_path, ("a.py",), {})

    body = "\n\ndef f():\n    return g(ts)\n"
    base = stamp("from server.code.domain.util import g\nimport tree_sitter as ts" + body)
    assert stamp(
        "from server.code.domain.extract.util import g\nimport tree_sitter as ts" + body
    ) == (base), "moving the imported module between packages"
    assert stamp("from .util import (\n    g,\n)\nimport tree_sitter as ts" + body) == base
    assert stamp("from server.code.domain.util import h as g\nimport tree_sitter as ts" + body) != (
        base
    ), "binding a different name is a code change"
    assert stamp("from server.code.domain.util import g\nimport tree_sitter" + body) != base
