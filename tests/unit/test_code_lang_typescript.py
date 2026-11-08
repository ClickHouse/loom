"""TypeScript / JavaScript fast-tier parsing.

Module-scoped qualified names, const-bound functions spanning their
statement, function-valued object members (tRPC procedures, route
handlers), const containers, interfaces / types / enums, test blocks as
suites and test functions, imports resolved through the probing ladder,
`new X()` as a call to X, external-package misses dropped.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_typescript")
pytest.importorskip("tree_sitter_javascript")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.edges_treesitter import (
    call_sites,
    collect_call_names,
    extract_edges_and_misses,
)
from server.code.domain.extract.lang_typescript import parse_typescript
from server.code.domain.extract.lang_typescript_imports import (
    drop_external_misses,
    import_bindings,
    import_candidates,
    resolve_imports,
)
from server.code.domain.extract.name_scope import gate_name_table, qualified_name_class

_ROUTER = b"""import { createTRPCRouter, protectedProcedure } from "@/src/server/api/trpc";
import { getObservationById } from "@langfuse/shared/src/server";
import { z } from "zod";

/** The observations router. */
export const observationsRouter = createTRPCRouter({
  byId: protectedProcedure
    .input(z.object({ id: z.string() }))
    .query(async ({ input }) => {
      const obs = await getObservationById(input.id);
      return format(obs);
    }),
  count: protectedProcedure.query(() => 1),
});

// formats one row
function format(o: unknown): string {
  return String(o);
}

export const helper = async (a: number, b?: string): Promise<number> => {
  const w = new Widget(a);
  return w.run() + a;
};

export class Widget extends Base implements Runnable {
  private n = 1;
  constructor(public a: number) {
    super();
  }
  run(): number {
    return this.inner() + this.a;
  }
  private inner() {
    return 1;
  }
}

export interface Props {
  a: string;
}
export type Score = { id: string };
export enum Color {
  Red,
  Green,
}
export const PostBody = z.object({
  id: z.string(),
});
const LIMIT = 10;
export default function Page() {
  return helper(LIMIT);
}
"""

_SPEC = b"""import { describe, expect, it, beforeEach } from "vitest";
import { helper } from "./router";

describe("helper", () => {
  beforeEach(async () => {
    await reset();
  });

  it("adds the width", async () => {
    expect(await helper(1)).toBe(2);
  });

  it.each([[1], [2]])("handles %s", async (n) => {
    expect(await helper(n)).toBeGreaterThan(n);
  });
});

async function reset() {}
"""


def _syms(pf):
    return {s.qualified_name: s for s in pf.symbols}


def test_qualified_names_are_module_scoped_and_kinds_follow_the_declaration() -> None:
    pf = parse_typescript("web/src/server/api/routers/observations.ts", _ROUTER)
    s = _syms(pf)
    mod = "web.src.server.api.routers.observations"
    assert s[f"{mod}::observationsRouter"].kind == "const"
    assert s[f"{mod}::observationsRouter"].doc_comment == "/** The observations router. */"
    assert s[f"{mod}::observationsRouter::byId"].kind == "function"
    assert s[f"{mod}::observationsRouter::byId"].signature == "({ input })"
    assert s[f"{mod}::observationsRouter::count"].kind == "function"
    assert s[f"{mod}::format"].doc_comment == "// formats one row"
    assert s[f"{mod}::helper"].signature == "(a: number, b?: string): Promise<number>"
    assert s[f"{mod}::Widget"].kind == "class"
    assert s[f"{mod}::Widget::constructor"].kind == "function"
    assert s[f"{mod}::Widget::run"].signature == "(): number"
    assert s[f"{mod}::Props"].kind == "interface"
    assert s[f"{mod}::Score"].kind == "type"
    assert s[f"{mod}::Color"].kind == "enum"
    assert s[f"{mod}::PostBody"].kind == "const"
    assert f"{mod}::LIMIT" not in s  # a one-line unexported scalar is no symbol
    assert s[f"{mod}::Page"].kind == "function"
    assert pf.inherits == [(f"{mod}::Widget", "Base"), (f"{mod}::Widget", "Runnable")]
    assert qualified_name_class(f"{mod}::helper") == "script"


def test_a_const_function_spans_its_whole_statement() -> None:
    pf = parse_typescript("a/b.ts", _ROUTER)
    helper = _syms(pf)["a.b::helper"]
    assert (helper.start_line, helper.end_line) == (21, 24)
    router = _syms(pf)["a.b::observationsRouter"]
    assert (router.start_line, router.end_line) == (6, 14)


def test_chunks_are_functions_and_skeletons_not_line_windows() -> None:
    pf = parse_typescript("a/b.ts", _ROUTER)
    chunks = chunk_file(pf)
    kinds = {c.kind for c in chunks}
    assert "function" in kinds and "class_skeleton" in kinds and "file_header" in kinds
    router = next(c for c in chunks if c.scope == "a.b::observationsRouter")
    assert router.kind == "class_skeleton"
    assert "body elided" in router.content  # the procedures are their own chunks
    assert any(c.scope == "a.b::observationsRouter::byId" and c.kind == "function" for c in chunks)
    widget = next(c for c in chunks if c.scope == "a.b::Widget")
    assert "// … body elided" in widget.content
    assert "private n = 1;" in widget.content


def test_test_blocks_are_suites_and_test_functions() -> None:
    pf = parse_typescript("web/src/__tests__/helper.servertest.ts", _SPEC)
    s = _syms(pf)
    mod = "web.src.__tests__.helper.servertest"
    assert s[f"{mod}::helper"].kind == "suite"
    assert s[f"{mod}::helper"].signature == 'describe("helper")'
    assert s[f"{mod}::helper::beforeEach"].kind == "function"
    assert s[f"{mod}::helper::test_adds_the_width"].signature == 'it("adds the width")'
    assert s[f"{mod}::helper::test_handles_s"].signature == 'it.each("handles %s")'
    assert s[f"{mod}::reset"].kind == "function"
    chunks = chunk_file(pf)
    assert sum(1 for c in chunks if c.kind == "other") == 0


def test_calls_new_and_member_calls_bind_by_the_ladder() -> None:
    pf = parse_typescript("a/b.ts", _ROUTER)
    names = collect_call_names(pf)
    assert {"getObservationById", "format", "Widget", "run", "inner", "helper"} <= names
    assert "object" not in names and "string" not in names  # zod chain verbs are noise
    edges, misses = extract_edges_and_misses(pf, {}, None)
    by = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1], e.edge_kind)
        for e in edges
    }
    assert ("byId", "format", "calls") in by  # same-file unique, 1.0
    assert ("helper", "Widget", "calls") in by  # `new Widget(a)` → the class
    assert ("run", "inner", "calls") in by  # method of the enclosing class
    assert ("Page", "helper", "calls") in by
    # The module-level `createTRPCRouter({...})` is a site of the file itself
    assert {m.callee_name for m in misses} == {"getObservationById", "createTRPCRouter"}
    assert {m.from_qualified_name for m in misses if m.callee_name == "createTRPCRouter"} == {
        pf.path
    }
    assert [n for _l, n in call_sites(pf)][:2] == ["createTRPCRouter", "getObservationById"]


def test_external_package_misses_are_dropped_and_repo_ones_kept() -> None:
    pf = parse_typescript("a/b.ts", _ROUTER)
    _edges, misses = extract_edges_and_misses(pf, {}, None)
    assert import_bindings(pf) == {
        "createTRPCRouter": "@/src/server/api/trpc",
        "protectedProcedure": "@/src/server/api/trpc",
        "getObservationById": "@langfuse/shared/src/server",
        "z": "zod",
    }
    # nothing resolved: the package import is external and its miss goes; the
    # module-level `createTRPCRouter({...})` binds through the
    # repository alias `@/…`, so its miss stays
    assert [m.callee_name for m in drop_external_misses(pf, misses, set())] == ["createTRPCRouter"]
    # the shared package resolved: its import is a repository binding, the miss stays
    kept = drop_external_misses(pf, misses, {"packages/shared/src/server/index.ts"})
    assert sorted(m.callee_name for m in kept) == ["createTRPCRouter", "getObservationById"]


def test_import_candidates_follow_the_compilers_probing() -> None:
    here = "web/src/features/x/y.ts"
    rel = import_candidates(here, "../lib/util")
    assert rel[:2] == ["web/src/features/lib/util.ts", "web/src/features/lib/util.tsx"]
    assert "web/src/features/lib/util/index.ts" in rel
    assert import_candidates(here, "./a.js")[0] == "web/src/features/x/a.ts"  # ESM `.js` → `.ts`
    alias = import_candidates(here, "@/src/utils/api")
    assert alias.index("web/src/utils/api.ts") < alias.index("web/src/features/src/utils/api.ts")
    assert alias[0] == "src/utils/api.ts"  # the repository root is tried first
    pkg = import_candidates(here, "@langfuse/shared/src/server")
    assert pkg[0] == "packages/shared/src/server.ts"
    assert "packages/shared/src/server/index.ts" in pkg
    assert "packages/shared/src/index.ts" in import_candidates(here, "@langfuse/shared")
    assert "packages/shared/src/features/query/index.ts" in import_candidates(
        here, "@langfuse/shared/query"
    )
    assert import_candidates(here, "react") == []
    assert import_candidates(here, "../../../../../x") == []  # above the repository root
    present = {"web/src/utils/api.ts", "packages/shared/src/index.ts"}
    assert resolve_imports(here, ["@/src/utils/api", "@langfuse/shared", "zod"], present) == [
        "web/src/utils/api.ts",
        "packages/shared/src/index.ts",
    ]


def test_parse_file_routes_the_new_extensions_and_javascript_parses_too() -> None:
    assert parse_file("x/a.tsx", b"export const A = () => <div />;\n").lang == "typescript"
    assert parse_file("x/a.mts", b"export function f() {}\n").lang == "typescript"
    js = parse_file(
        "scripts/run.mjs",
        b"export async function main() { await go(); }\nmodule.exports = { main };\n",
    )
    assert js is not None and js.lang == "javascript"
    assert {s.name for s in js.symbols} == {"main", "default"}
    assert parse_file("web/styles.css", b":root { --x: 1; }\n").lang == "css"
    assert parse_file("docs/a.mdx", b"# Title\n").lang == "doc"
    assert parse_file("web/img.woff2", b"\x00\x01") is None


def test_language_class_gate_keeps_typescript_with_the_script_class() -> None:
    table = {"seq": [("id1", "DB::Foo::seq", "function"), ("id2", "web.src.a::seq", "function")]}
    assert gate_name_table(table, "typescript") == {"seq": [("id2", "web.src.a::seq", "function")]}


def test_test_files_and_declaration_files_are_noise_paths_for_symbol_ranking() -> None:
    from server.code.store.index import is_noise_path

    assert is_noise_path("web/src/features/traces/fns/treeBuilding.clienttest.ts")
    assert is_noise_path("web/src/__tests__/server/queryBuilder.servertest.ts")
    assert is_noise_path("worker/src/__tests__/batchExport.test.ts")
    assert is_noise_path("web/src/components/ui/Button.stories.tsx")
    assert is_noise_path("web/types/next-auth.d.ts")
    assert not is_noise_path("web/src/features/traces/fns/treeBuilding.ts")
    assert not is_noise_path("packages/shared/prisma/schema.prisma")
    assert not is_noise_path("packages/shared/src/server/repositories/scores.ts")


_REFS = b"""import type { Score } from "./types";
import { Badge } from "@/components/ui/badge";
export interface Props { score: Score; items: Array<Item>; cb: (x: Foo) => Bar }
export function View({ score }: Props): JSX.Element {
  const s: Score | null = useScore<Score>(score as Score);
  return <Badge variant="x"><Inner.Part a={1} /><div /><Local /></Badge>;
}
export const Local = () => <span />;
export type Wrapped = Partial<Props> & { extra: Score };
export class Svc implements Runner { run(a: Score): Promise<Bar> { return go(); } }
"""


def test_reference_sites_and_edges_for_types_and_jsx() -> None:
    from server.code.domain.extract.lang_typescript_refs import reference_edges, reference_sites

    pf = parse_typescript("web/src/x.tsx", _REFS)
    sites = reference_sites(pf)
    names = [(n, k) for _l, n, k in sites]
    assert ("Score", "type") in names and ("Item", "type") in names and ("Foo", "type") in names
    assert ("Props", "type") in names and ("Runner", "type") in names
    jsx = {
        n for n, k in names if k.startswith("jsx")
    }  # a member element carries its root in the kind
    assert {"Badge", "Part", "Local"} <= jsx
    assert not any(n in {"Array", "Partial", "Promise", "JSX", "Element", "div"} for n, _ in names)
    assert not any(n == "Wrapped" or n == "Svc" for n, _ in names)  # the declared names
    table = {
        "Score": [("s1", "web.src.types::Score", "type")],
        "Bar": [("b1", "web.src.a::Bar", "interface"), ("b2", "web.src.b::Bar", "type")],
        "Badge": [("bd", "web.src.components.ui.badge::Badge", "function")],
        "Runner": [("r1", "web.src.r::Runner", "interface")],
    }
    edges = reference_edges(pf, sites, table)
    shapes = {
        (
            e.from_qualified_name.split("::")[-1],
            e.edge_kind,
            e.to_qualified_name.split("::")[-1],
            e.confidence,
        )
        for e in edges
    }
    assert ("Props", "uses_type", "Score", 0.6) in shapes  # an interface's field type
    assert ("View", "uses_type", "Props", 1.0) in shapes  # same-file interface
    assert ("View", "calls", "Badge", 0.6) in shapes  # a rendered component, repo-unique
    assert ("View", "calls", "Local", 1.0) in shapes  # a rendered component, same file
    assert ("Svc", "uses_type", "Runner", 0.6) in shapes
    assert not any(t == "Bar" for _, _, t, _ in shapes)  # two definitions: ambiguous, no edge
    assert not any(t == "Part" for _, _, t, _ in shapes)  # nobody defines it


_PKG_JSON = """{
  "name": "@langfuse/shared",
  "main": "./dist/src/index.js",
  "exports": {
    ".": {"import": "./dist/src/index.js", "require": "./dist/src/index.js"},
    "./query": {"import": "./dist/src/features/query/index.js"},
    "./features/*": {"types": "./dist/src/features/*/index.d.ts", "default": "./dist/src/features/*/index.js"},
    "./private/*": null
  }
}"""


def test_exports_table_resolves_subpaths_the_probing_ladder_cannot() -> None:
    from server.code.domain.extract.lang_typescript_imports import (
        exports_candidates,
        manifest_view,
        package_dirs_for,
        package_subpath,
        unresolved_package_specs,
    )

    assert package_dirs_for("@langfuse/shared/query") == [
        "packages/shared",
        "shared",
        "apps/shared",
        "libs/shared",
    ]
    assert package_subpath("@langfuse/shared/query") == "./query" and package_subpath("zod") == "."
    pkg = manifest_view(_PKG_JSON)
    cands = exports_candidates("@langfuse/shared/query", "packages/shared", pkg)
    assert cands[0] == "packages/shared/dist/src/features/query/index.ts"
    assert "packages/shared/src/features/query/index.ts" in cands  # `dist/` → `src/`
    assert exports_candidates("@langfuse/shared", "packages/shared", pkg)[0] == (
        "packages/shared/dist/src/index.ts"
    )
    star = exports_candidates("@langfuse/shared/features/monitors", "packages/shared", pkg)
    assert "packages/shared/src/features/monitors/index.ts" in star  # a `*` pattern, `types` first
    assert exports_candidates("@langfuse/shared/private/x", "packages/shared", pkg) == []
    assert exports_candidates("@langfuse/shared/nope", "packages/shared", pkg) == []
    assert manifest_view("not json") is None
    present = {"packages/shared/src/index.ts"}
    specs = ["@langfuse/shared", "@langfuse/shared/query", "./x", "zod", "@/src/a"]
    assert unresolved_package_specs("web/src/a.ts", specs, present) == [
        "@langfuse/shared/query",
        "zod",
    ]


def test_a_definition_both_tiers_hold_is_one_candidate() -> None:
    from server.code.domain.extract.edges_treesitter import candidates_for_call

    table = {
        "helper": [
            ("id-parser", "a.b::helper", "function"),
            ("id-scip", "a.b::helper", "function"),
        ],
        "run": [("r1", "a.b::X::run", "function"), ("r2", "a.c::Y::run", "function")],
    }
    assert candidates_for_call(table, "helper", "helper") == [
        ("id-parser", "a.b::helper", "function")
    ]
    assert len(candidates_for_call(table, "run", "run")) == 2  # two definitions stay ambiguous
    assert candidates_for_call(table, "X::run", "run") == [("r1", "a.b::X::run", "function")]
    pf = parse_typescript("a/b.ts", _ROUTER)
    edges, _misses = extract_edges_and_misses(
        pf,
        {
            "getObservationById": [
                ("p1", "packages.shared.src.server::getObservationById", "function"),
                ("s1", "packages.shared.src.server::getObservationById", "function"),
            ]
        },
        None,
    )
    # The call binds through its import (1.0), not by repo-wide uniqueness
    # (0.6); either way one edge, one definition
    assert any(
        e.to_qualified_name.endswith("::getObservationById") and e.confidence >= 0.6 for e in edges
    )


def test_barrel_reexports_are_followed_one_level() -> None:
    from server.code.domain.extract.lang_typescript_imports import barrel_targets, reexport_specs

    barrel = """export * from "./dataModel";
export { QueryBuilder, type Plan } from "./builder";
export type { Widget } from "./types";
export * as util from "./util";
export * from "zod";
export const local = 1;
import { x } from "./notAnExport";
"""
    assert reexport_specs(barrel) == ["./dataModel", "./builder", "./types", "./util", "zod"]
    present = {
        "packages/shared/src/features/query/dataModel.ts",
        "packages/shared/src/features/query/builder/index.tsx",
        "packages/shared/src/features/query/index.ts",
    }
    assert barrel_targets("packages/shared/src/features/query/index.ts", barrel, present) == [
        "packages/shared/src/features/query/dataModel.ts",
        "packages/shared/src/features/query/builder/index.tsx",
    ]  # `./types` / `./util` are not indexed, `zod` is a package
    facade = "".join(f'export * from "./m{i}";\n' for i in range(17))
    everything = {f"pkg/src/m{i}.ts" for i in range(17)}
    assert barrel_targets("pkg/src/index.ts", facade, everything) == []  # a facade is not followed
    small = "".join(f'export * from "./m{i}";\n' for i in range(16))
    assert len(barrel_targets("pkg/src/index.ts", small, everything)) == 16


def test_a_component_declared_through_a_wrapper_is_a_function_unit() -> None:
    """`forwardRef<P>(fn)` / `memo(fn)` / `observer(memo(fn))`
    declare a component; the unit is a function (its render sites bind to
    it, the calls inside it attribute to it), a wrapped non-function stays
    a const."""
    src = b"""import { forwardRef, memo } from 'react';
import { helper } from './util';
export const BaseButton = forwardRef<HTMLButtonElement, Props>(
  ({ label, ...props }, ref) => {
    const x = helper(label);
    return <button ref={ref}>{x}</button>;
  }
);
const Icon = memo(function Icon(props: IconProps) {
  return <BaseButton label={props.name} />;
});
export const Wrapped = observer(memo(() => <Icon name="x" />));
export const table = memo(Object.keys(x));
export { Icon };
"""
    pf = parse_file("src/a.tsx", src)
    kinds = {s.name: (s.kind, s.start_line, s.end_line) for s in pf.symbols}
    assert kinds["BaseButton"] == ("function", 3, 8)
    assert kinds["Icon"] == ("function", 9, 11)
    assert kinds["Wrapped"][0] == "function"
    assert kinds["table"][0] == "const"
    # the render site inside Icon binds to the wrapped BaseButton, same file
    from server.code.domain.extract.lang_typescript_refs import reference_edges, reference_sites

    sites = reference_sites(pf)
    edges = reference_edges(pf, sites, {})
    assert [(e.from_qualified_name, e.to_qualified_name, e.edge_kind) for e in edges] == [
        ("src.a::Icon", "src.a::BaseButton", "calls"),
        ("src.a::Wrapped", "src.a::Icon", "calls"),
    ]


def test_reference_edges_bind_through_the_import_first() -> None:
    """`<Cell>` in Grid.tsx binds to the `./Cell` the file
    imports, not to the `Cell` a story or a test declares (three same-named
    definitions made the repository-unique rung give up); a name imported
    from a library never binds to a repo-unique namesake; a barrel import
    keeps the repository-unique rung."""
    from server.code.domain.extract.lang_typescript_imports import import_bindings
    from server.code.domain.extract.lang_typescript_refs import reference_edges, reference_sites

    src = b"""import { Cell } from './Cell';
import { Button } from '@radix-ui/react-button';
import { Badge } from '@/components';
import * as RadixTooltip from '@radix-ui/react-tooltip';
import * as Own from './Own';
export const Grid = forwardRef<HTMLDivElement, Props>((props, ref) => (
  <div><Cell /><Button /><Badge /><RadixTooltip.Arrow /><Own.Arrow /></div>
));
"""
    pf = parse_file("src/components/Grid/Grid.tsx", src)
    table = {
        "Cell": [
            ("c1", "src.components.Grid.Cell::Cell", "function"),
            ("c2", "src.components.Grid.Grid.stories::Cell", "function"),
            ("c3", "src.components.Grid.Grid.test::Cell", "function"),
        ],
        "Button": [("b1", "src.components.Button.Button::Button", "function")],
        "Badge": [("d1", "src.components.Badge.Badge::Badge", "function")],
        "Arrow": [
            ("a1", "src.components.Grid.Own::Arrow", "function"),
            ("a2", "src.components.Tooltip.Arrow::Arrow", "function"),
        ],
    }
    edges = reference_edges(pf, reference_sites(pf), table, import_bindings(pf))
    got = {(e.to_qualified_name, round(e.confidence, 1)) for e in edges}
    # `<Own.Arrow>` binds through the namespace import; `<RadixTooltip.Arrow>` never
    assert got == {
        ("src.components.Grid.Cell::Cell", 1.0),
        ("src.components.Badge.Badge::Badge", 0.6),
        ("src.components.Grid.Own::Arrow", 1.0),
    }
    # without bindings the plain ladder: Cell ambiguous, Button and Badge repo-unique
    got_old = {e.to_qualified_name for e in reference_edges(pf, reference_sites(pf), table)}
    assert got_old == {"src.components.Button.Button::Button", "src.components.Badge.Badge::Badge"}


def test_a_typeof_query_is_a_uses_type_edge_in_the_parser_tier() -> None:
    """The parser tier records `typeof Button` as uses_type of
    the value; the render site stays the only call."""
    from server.code.domain.extract.lang_typescript_imports import import_bindings
    from server.code.domain.extract.lang_typescript_refs import reference_edges, reference_sites

    src = b"""import { Button } from './Button';
type Props = { assist: ComponentProps<typeof Button>['x'] };
export function Step(p: Props) { return <Button />; }
"""
    pf = parse_file("src/a.tsx", src)
    sites = reference_sites(pf)
    assert (2, "Button", "typeof") in sites and (3, "Button", "jsx") in sites
    table = {"Button": [("b1", "src.Button::Button", "function")]}
    edges = reference_edges(pf, sites, table, import_bindings(pf))
    assert sorted(
        (e.edge_kind, e.line) for e in edges if e.to_qualified_name == "src.Button::Button"
    ) == [
        ("calls", 3),
        ("uses_type", 2),
    ]
