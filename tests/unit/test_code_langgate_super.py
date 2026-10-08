"""A repo-wide rung binds only the caller's language, for
every script-class language; `super().m()` binds the base class's method."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.name_scope import (
    defined_in_language,
    gate_name_table,
    go_package_dirs,
    scope_stems,
)


def _calls(edges):
    return {
        (e.line, e.to_qualified_name, round(e.confidence, 2))
        for e in edges
        if e.edge_kind == "calls"
    }


def test_scope_stems_keep_dotted_file_names():
    assert scope_stems("a.b.c") == ["a/b/c", "a/b.c", "a.b.c"]
    assert scope_stems("a") == ["a"]


def test_defined_in_language_reads_the_present_paths():
    present = {
        "web/src/codec.ts",
        "svc/util/text.py",
        "tools/run.sh",
        "tools/deploy",
        "pkg/db/client.go",
    }
    assert defined_in_language("web.src.codec::decode", "typescript", present)
    assert not defined_in_language("web.src.codec::decode", "python", present)
    assert defined_in_language("svc.util.text::decode", "python", present)
    assert not defined_in_language("svc.util.text::decode", "javascript", present)
    assert defined_in_language("tools.run::helper", "shell", present)
    assert defined_in_language("tools.deploy::helper", "shell", present), "an extensionless script"
    assert defined_in_language("pkg.db.db::Client::Query", "go", present, go_package_dirs(present))
    assert not defined_in_language("svc.util.text::decode", "go", present, go_package_dirs(present))
    assert defined_in_language("DB::Foo::bar", "python", present), "a native name is never gated"
    assert defined_in_language("x.y::z", "java", present), "no table for the language: never gated"


TS_FILES = {
    "web/src/app.ts": b"import { render } from './view';\nexport function main() { render(1); decode('x'); }\n",
    "web/src/view.ts": b"export function render(n: number) { return n; }\n",
    "svc/text.py": b"def decode(b):\n    return b\n",
}


def _ts_edges(with_present: bool):
    present = set(TS_FILES)
    parsed = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    for rel, src in TS_FILES.items():
        pf = parse_file(rel, src)
        if pf is None:
            continue
        parsed[rel] = pf
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    pf = parsed["web/src/app.ts"]
    from server.code.domain.extract.lang_typescript_imports import resolve_imports

    pf.includes = resolve_imports(pf.path, list(pf.includes), present)
    ctx = {"present": present} if with_present else None
    edges, _ = extract_edges_and_misses(
        pf, gate_name_table(name_table, "typescript"), None, None, ctx
    )
    return _calls(edges)


def test_a_typescript_call_never_binds_a_python_definition():
    pytest.importorskip("tree_sitter_typescript")
    gated = _ts_edges(with_present=True)
    assert any(q == "web.src.view::render" for _l, q, _ in gated)
    assert not any(q.startswith("svc.text") for _l, q, _ in gated), (
        "`decode()` is not the Python file's"
    )
    ungated = _ts_edges(with_present=False)
    assert any(q == "svc.text::decode" for _l, q, _ in ungated), (
        "without the present set the old rung binds it"
    )


SUPER_FILES = {
    "lib/base.py": b"class Base:\n    def __init__(self, x):\n        self.x = x\n\n    def run(self):\n        return 1\n\n\nclass Other:\n    def run(self):\n        return 2\n",
    "lib/child.py": (
        b"from lib.base import Base\n\n\nclass Child(Base):\n    def __init__(self, x):\n        super().__init__(x)\n\n"
        b"    def run(self):\n        return super().run() + 1\n\n\nclass Loose:\n    def run(self):\n        return super().run()\n"
    ),
}


def _super_edges():
    present = set(SUPER_FILES)
    parsed = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    for rel, src in SUPER_FILES.items():
        pf = parse_file(rel, src)
        parsed[rel] = pf
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    pf = parsed["lib/child.py"]
    from server.code.domain.extract.lang_python import resolve_imports

    pf.includes = resolve_imports(pf.path, list(pf.includes), present)
    ctx = {"present": present, "py_present": present}
    edges, misses = extract_edges_and_misses(
        pf, gate_name_table(name_table, "python"), None, None, ctx
    )
    return _calls(edges), misses


def test_super_binds_the_base_classs_method():
    calls, misses = _super_edges()
    assert any(line == 6 and q == "lib.base::Base::__init__" for line, q, _ in calls)
    assert any(line == 9 and q == "lib.base::Base::run" for line, q, _ in calls), (
        "not Other.run, not Child.run"
    )
    assert not any(line == 14 for line, _q, _ in calls), (
        "a class with no base: super() binds nothing"
    )
