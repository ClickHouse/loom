"""Two Python import forms around an optional dependency, each isolated:
an import in an except clause is an include, and a call through
`from m import f as g` binds the member `f` of `m`. Together they let
python/imports/app/forms.py bind `fast_helper()` in both branches."""

from __future__ import annotations

import posixpath

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_python import resolve_imports
from server.code.domain.extract.name_scope import gate_name_table
from tests.code_lanes.corpus import ROOT

FORMS = "python/imports/app/forms.py"


def test_an_import_in_an_except_clause_is_an_include() -> None:
    with open(posixpath.join(ROOT, FORMS), "rb") as fh:
        pf = parse_file(FORMS, fh.read())
    assert pf is not None
    assert ".local" in pf.includes, pf.includes


def test_a_call_through_a_from_import_alias_binds_the_member() -> None:
    files = {
        "pkg/__init__.py": b"",
        "pkg/impl.py": b"def helper():\n    return 1\n",
        "app/main.py": b"from pkg.impl import helper as fast\n\n\ndef run():\n    fast()\n",
    }
    parsed = {rel: parse_file(rel, src) for rel, src in files.items()}
    names: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        assert pf is not None
        for s in pf.symbols:
            names.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    pf = parsed["app/main.py"]
    assert pf is not None
    pf.includes = [p for p in resolve_imports("app/main.py", list(pf.includes), set(files))]
    edges, _ = extract_edges_and_misses(
        pf, gate_name_table(names, "python"), None, None, {"py_present": set(files)}
    )
    assert any(
        e.edge_kind == "calls" and e.to_qualified_name == "pkg.impl::helper" for e in edges
    ), [(e.edge_kind, e.to_qualified_name) for e in edges]
