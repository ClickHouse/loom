"""The forms any TypeScript or JavaScript repository
writes — overload signatures, class expressions, IIFE module scope,
CommonJS `exports.f`, dynamic imports — index and bind."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_typescript_imports import import_bindings


def _calls(pf, table=None):
    edges, misses = extract_edges_and_misses(pf, table or {}, None)
    return (
        {
            (
                e.from_qualified_name.split("::")[-1],
                e.to_qualified_name.split("::")[-1],
                e.confidence,
            )
            for e in edges
            if e.edge_kind == "calls"
        },
        {(m.callee_name, m.disposition) for m in misses},
    )


def test_overload_signatures_fold_into_the_implementation() -> None:
    pf = parse_file(
        "d.ts",
        b"export function f(x: string): string;\nexport function f(x: number): number;\n"
        b"export function f(x: any) { return x; }\nexport function g() { return f(1); }\n"
        b"export interface I { m(): void }\nexport abstract class A { abstract n(): void }\n",
    )
    names = [(s.kind, s.name, s.has_body) for s in pf.symbols]
    assert names.count(("function", "f", True)) == 1 and ("function", "f", False) not in names
    assert ("function", "n", False) in names  # an abstract method has no implementation: kept
    edges, misses = _calls(pf)
    assert edges == {("g", "f", 1.0)} and misses == set()


def test_a_class_expression_is_the_class_its_binding_names_with_its_members() -> None:
    pf = parse_file(
        "j.ts",
        b"export const Foo = class Bar {\n  get value() { return 1; }\n  run() { return init(); }\n};\n"
        b"function init() {}\n",
    )
    assert [(s.kind, s.qualified_name) for s in pf.symbols] == [
        ("class", "j::Foo"),
        ("function", "j::Foo::value"),
        ("function", "j::Foo::run"),
        ("function", "j::init"),
    ]
    assert _calls(pf)[0] == {("run", "init", 1.0)}


def test_an_iife_at_statement_level_is_module_scope() -> None:
    pf = parse_file(
        "x.js",
        b"(function () {\n  function Widget() {}\n  Widget.prototype.render = function () { return draw() }\n"
        b"  function draw() {}\n})()\n!function () { function z() {} }()\n",
    )
    assert [s.name for s in pf.symbols] == ["Widget", "render", "draw", "z"]
    assert _calls(pf)[0] == {("render", "draw", 1.0)}
    # top-level await form (a module): the async body is module scope too
    ts = parse_file("y.mts", b"await (async () => { function w() {} })();\nexport const k = 1;\n")
    assert "w" in [s.name for s in ts.symbols]


def test_exports_dot_f_calls_this_modules_f_whatever_the_noise_list_says() -> None:
    pf = parse_file(
        "i.js",
        b"exports.parse = function parse(s) { return s }\nexports.run = () => exports.parse('x')\n"
        b"module.exports.check = () => module.exports.parse('y')\n",
    )
    edges, _ = _calls(pf)
    assert ("run", "parse", 1.0) in edges and ("check", "parse", 1.0) in edges


def test_dynamic_imports_bind_a_namespace_and_destructured_names() -> None:
    pf = parse_file(
        "q.ts",
        b"const mod = await import('./lazy');\nconst { go, stop: halt } = await import('./lazy2');\n"
        b"export async function run() { return mod.start() + go() + halt(); }\n",
    )
    assert import_bindings(pf) == {"mod": "./lazy", "go": "./lazy2", "halt": "./lazy2"}
    assert set(pf.includes) >= {"./lazy", "./lazy2"}
