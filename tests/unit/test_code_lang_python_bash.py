"""Python + bash fast-tier parsing: symbols, edges, chunks.

The property that matters most is pinned first: qualified names are
module-prefixed, because code_symbols keys on (qualified_name, symbol_id)
and a test corpus defines main()/fixtures with identical names AND
identical () signatures in hundreds of files — path-free names would
silently clobber each other under FINAL.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter_bash")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.chunker import chunk_file
from server.code.domain.extract.edges_treesitter import collect_call_names, extract_edges
from server.code.domain.extract.lang_bash import parse_bash
from server.code.domain.extract.lang_python import module_scope, parse_python

_PY = b'''\
import os
from helpers.cluster import ClickHouseCluster

# starts the shared cluster
def start_cluster():
    """Boot the fixture cluster."""
    helper()
    return ClickHouseCluster(os.getcwd())


def helper():
    pass


class TestBackup:
    def test_restore(self):
        self.check(start_cluster())

    def check(self, c):
        pass
'''

_SH = b"""\
#!/usr/bin/env bash
source ./common.sh

function prepare() {
    setup_env
}

setup_env() {
    echo ready
}

prepare
"""


def test_python_symbols_are_module_scoped() -> None:
    pf = parse_python("tests/integration/test_backup/test.py", _PY)
    names = {s.qualified_name: s for s in pf.symbols}
    mod = "tests.integration.test_backup.test"
    assert f"{mod}::start_cluster" in names
    assert f"{mod}::TestBackup" in names
    assert f"{mod}::TestBackup::test_restore" in names
    assert names[f"{mod}::start_cluster"].doc_comment == "Boot the fixture cluster."
    assert names[f"{mod}::TestBackup"].kind == "class"
    # two files defining start_cluster() must NOT share a symbol_id
    other = parse_python("tests/integration/test_other/test.py", _PY)
    a = names[f"{mod}::start_cluster"].id
    b = next(s for s in other.symbols if s.name == "start_cluster").id
    assert a != b


def test_python_imports_and_inherits() -> None:
    pf = parse_python("t/x.py", b"import json\nclass A(Base):\n    pass\n")
    assert "json" in pf.includes
    assert pf.inherits and pf.inherits[0][1] == "Base"


def test_python_edges_same_file_and_method() -> None:
    pf = parse_python("tests/integration/test_backup/test.py", _PY)
    edges = extract_edges(pf, {})
    calls = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1])
        for e in edges
        if e.edge_kind == "calls"
    }
    # start_cluster -> helper (same-file unique, 1.0)
    assert ("start_cluster", "helper") in calls
    # test_restore -> check (enclosing-class method) and -> start_cluster
    assert ("test_restore", "check") in calls
    assert ("test_restore", "start_cluster") in calls
    # python noise (print/len/...) never queried against the name table
    assert "getcwd" in collect_call_names(pf)  # real name, kept
    assert "print" not in collect_call_names(pf)


def test_python_chunks_are_semantic() -> None:
    pf = parse_python("tests/integration/test_backup/test.py", _PY)
    chunks = chunk_file(pf)
    kinds = {c.kind for c in chunks}
    assert "function" in kinds and "class_skeleton" in kinds
    fn = next(c for c in chunks if c.scope.endswith("::start_cluster"))
    assert "def start_cluster" in fn.content and fn.symbol_ids
    skel = next(c for c in chunks if c.kind == "class_skeleton")
    assert "# … body elided" in skel.content  # python comment style, not /* */


def test_bash_symbols_edges_and_sources() -> None:
    pf = parse_bash("tests/queries/shell/run.sh", _SH)
    names = {s.qualified_name for s in pf.symbols}
    mod = module_scope("tests/queries/shell/run.sh")
    assert f"{mod}::prepare" in names and f"{mod}::setup_env" in names
    assert pf.includes == ["./common.sh"]
    edges = extract_edges(pf, {})
    calls = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1])
        for e in edges
        if e.edge_kind == "calls"
    }
    assert ("prepare", "setup_env") in calls
    # bash noise (echo) is never an edge
    assert not any(t == "echo" for _, t in calls)


def test_parse_file_dispatch_and_sql_follows_its_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    py = parse_file("a/b.py", b"def f():\n    pass\n")
    assert py is not None and py.symbols and py.lang == "python"
    sh = parse_file("a/b.sh", b"f() { true; }\n")
    assert sh is not None and sh.symbols and sh.lang == "shell"
    # A SQL file has no tree-sitter tree either way. The statement-records gate
    # decides what it yields: off (the default) keeps it chunk-only with no
    # symbols; on makes every statement a symbol and DDL names its objects.
    # The test pins both, so it holds whether or not the process loaded a .env.
    monkeypatch.setattr(settings, "code_sql_statement_records", False)
    sql = parse_file("a/b.sql", b"SELECT 1;\n")
    assert sql is not None and sql.lang == "sql" and sql.tree is None and not sql.symbols
    monkeypatch.setattr(settings, "code_sql_statement_records", True)
    sql = parse_file("a/b.sql", b"SELECT 1;\n")
    assert sql is not None and sql.lang == "sql" and sql.tree is None
    assert sql.symbols and all(
        s.kind in ("statement", "table", "view", "function") for s in sql.symbols
    )


def test_shebang_sniffing_for_extensionless_scripts() -> None:
    py = parse_file("tests/clickhouse-test", b"#!/usr/bin/env python3\ndef f():\n    pass\n")
    assert py is not None and py.lang == "python" and py.symbols
    sh = parse_file("ci/run", b"#!/bin/bash\nf() { true; }\n")
    assert sh is not None and sh.lang == "shell" and sh.symbols
    lic = parse_file("LICENSE", b"Apache License\n")  # text, no symbols
    assert lic is not None and lic.lang == "text" and lic.symbols == []


def test_module_scope_shapes() -> None:
    assert module_scope("tests/integration/test_x/test.py") == "tests.integration.test_x.test"
    assert module_scope("ci/jobs/scripts/run.sh") == "ci.jobs.scripts.run"


def test_bodies_none_serves_cards_only() -> None:
    from server.code.domain.units import CodeUnit, budget_units

    units = [
        CodeUnit(
            unit_id=f"u{i}",
            path="src/A.cpp",
            start_line=1,
            end_line=9,
            symbol=f"f{i}",
            kind="function",
            signature="void f()",
            body="int x = 0;\n" * 180,
        )  # ~2K chars: a real function, not a stub
        for i in range(5)
    ]
    full, t_full = budget_units(units, 6000)
    cards, t_cards = budget_units(units, 6000, bodies="none")
    assert any(not u.elided and u.body for u in full)
    assert all(u.elided and not u.body for u in cards)
    assert t_cards < t_full / 3  # the measured survey-mode economics
