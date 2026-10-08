"""What four more repositories' compiler-tier oracles
exposed — package-aware import roots, methods inherited through the
heritage clauses, fluent chains typed link by link, fixture-typed test
parameters, and no same-file guess for an untyped member receiver."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_python")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import callee_targets, extract_edges_and_misses
from server.code.domain.extract.lang_python import first_present_candidate, resolve_imports
from server.code.domain.extract.name_scope import gate_name_table


def _corpus(files: dict[str, bytes]):
    present = set(files)
    parsed = {}
    name_table: dict[str, list[tuple[str, str, str]]] = {}
    returns: dict[str, str] = {}
    for rel, src in files.items():
        pf = parse_file(rel, src)
        parsed[rel] = pf
        for s in pf.symbols:
            name_table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
            if s.return_type:
                returns[s.qualified_name] = s.return_type
    return present, parsed, name_table, returns


def _edges(files: dict[str, bytes], rel: str):
    present, parsed, name_table, returns = _corpus(files)
    pf = parsed[rel]
    pf.includes = [p for p in resolve_imports(rel, list(pf.includes), present) if p != rel]
    gated = gate_name_table(name_table, "python")
    ctx = {"present": present, "py_present": present}
    targets = callee_targets(pf, gated, ctx)
    callee_returns = {c: returns[q] for c, q in targets.items() if returns.get(q)}
    if callee_returns:
        more = callee_targets(pf, gated, ctx, callee_returns)
        callee_returns.update({c: returns[q] for c, q in more.items() if returns.get(q)})
    edges, misses = extract_edges_and_misses(pf, gated, None, callee_returns or None, ctx)
    return {
        (e.line, e.to_qualified_name, round(e.confidence, 2))
        for e in edges
        if e.edge_kind == "calls"
    }, misses


def test_a_package_member_does_not_import_its_sibling_by_bare_name():
    present = {
        "pydantic/__init__.py",
        "pydantic/dataclasses.py",
        "pydantic/main.py",
        "tools/run.py",
        "tools/helpers.py",
    }
    # inside the package `pydantic/`, `import dataclasses` is the standard library's
    assert first_present_candidate("pydantic/main.py", "dataclasses", present) == ""
    # a script directory without __init__.py still reaches its neighbour
    assert first_present_candidate("tools/run.py", "helpers", present) == "tools/helpers.py"
    # the repository root always applies
    assert (
        first_present_candidate("pydantic/main.py", "pydantic.dataclasses", present)
        == "pydantic/dataclasses.py"
    )


INHERIT = {
    "lib/base.py": b"class Mixin:\n    def __init__(self, x):\n        self.x = x\n\n    def log(self, m):\n        return m\n",
    "lib/mid.py": b"from lib.base import Mixin\n\n\nclass Mid(Mixin):\n    pass\n\n\nclass Leaf(Mid):\n    def __init__(self):\n        super().__init__(1)\n\n    def go(self):\n        return self.log('x')\n",
}


def test_inherited_methods_bind_through_the_heritage_clauses():
    calls, _ = _edges(INHERIT, "lib/mid.py")
    assert any(line == 10 and q == "lib.base::Mixin::__init__" for line, q, _ in calls), (
        "super() two levels up, within the file"
    )
    assert any(line == 13 and q == "lib.base::Mixin::log" for line, q, _ in calls), (
        "self.log() the class inherits"
    )


CHAIN = {
    "flow/pipe.py": b"class Pipe:\n    def gt(self, n) -> 'Pipe':\n        return self\n\n    def lt(self, n) -> 'Pipe':\n        return self\n\n\ndef validate_as(t) -> Pipe:\n    return Pipe()\n",
    "flow/use.py": b"from flow.pipe import validate_as\n\n\ndef rule():\n    return validate_as(int).gt(0).lt(9)\n",
}


def test_a_fluent_chain_types_link_by_link():
    calls, _ = _edges(CHAIN, "flow/use.py")
    assert any(line == 5 and q == "flow.pipe::Pipe::gt" for line, q, _ in calls), (
        "`validate_as(int).gt` by the callee's return"
    )
    assert any(line == 5 and q == "flow.pipe::Pipe::lt" for line, q, _ in calls), (
        "`.gt(0).lt` by `Pipe::gt`'s return"
    )


FIXTURES = {
    "tests/conftest.py": b"import pytest\nfrom lib.locker import Locker\n\n\n@pytest.fixture\ndef locker() -> Locker:\n    return Locker()\n\n\n@pytest.fixture\ndef gen_locker():\n    yield Locker()\n",
    "lib/locker.py": b"class Locker:\n    def lock(self):\n        return 1\n\n\nclass Other:\n    def lock(self):\n        return 2\n",
    "tests/test_x.py": b"def test_a(locker):\n    locker.lock()\n\n\ndef test_b(gen_locker):\n    gen_locker.lock()\n",
}


def test_a_test_parameter_is_typed_by_its_fixtures_yield():
    calls, misses = _edges(FIXTURES, "tests/test_x.py")
    assert any(line == 2 and q == "lib.locker::Locker::lock" for line, q, _ in calls), (
        "`locker` typed by the fixture's return annotation"
    )
    assert not any(line == 6 for line, _q, _ in calls), (
        "an unannotated fixture types nothing: ambiguous, no guess"
    )


def test_a_yield_fixtures_annotation_is_unwrapped():
    from server.code.domain.extract.lang_python import parse_python

    pf = parse_python(
        "tests/conftest.py",
        b"import pytest\nfrom typing import Iterator\n\n\n@pytest.fixture\ndef c() -> Iterator[Client]:\n    yield Client()\n",
    )
    fx = next(s for s in pf.symbols if s.name == "c")
    assert fx.kind == "fixture" and fx.return_type == "Client"


DOUBLE = {
    "svc/real.py": b"class Lease:\n    def acquire(self):\n        return 1\n",
    "tests/test_lease.py": b"from svc.real import Lease\n\n\nclass _Fake:\n    def acquire(self):\n        return 0\n\n\ndef test_it(lease):\n    lease.acquire()\n    real = Lease()\n    real.acquire()\n",
}


def test_an_untyped_member_receiver_takes_no_same_file_guess():
    calls, misses = _edges(DOUBLE, "tests/test_lease.py")
    assert not any(line == 10 for line, _q, _ in calls), "`lease.acquire()` is not the double's"
    assert any(line == 12 and q == "svc.real::Lease::acquire" for line, q, _ in calls), (
        "a typed receiver still binds"
    )


CROSS = {
    "sa/compiler.py": b"class Compiled:\n    def process(self, x):\n        return x\n\n\nclass SQLCompiler(Compiled):\n    pass\n",
    "sa/other.py": b"class Other:\n    def process(self, x):\n        return x\n",
    "sa/mssql.py": (
        b"from sa.compiler import SQLCompiler\n\n\nclass MSCompiler(SQLCompiler):\n    def visit(self, x):\n"
        b"        return self.process(x)\n\n    def dunder(self):\n        return object.__getattribute__(self, 'x')\n"
    ),
}


def test_a_method_two_files_up_binds_through_the_inherits_rows():
    present, parsed, name_table, _returns = _corpus(CROSS)
    from server.code.domain.extract.edges_treesitter import extract_edges

    gated = gate_name_table(name_table, "python")
    class_bases: dict[str, list[str]] = {}
    for pf in parsed.values():
        for e in extract_edges(pf, gated):
            if e.edge_kind == "inherits":
                class_bases.setdefault(e.from_qualified_name, []).append(e.to_qualified_name)
    assert class_bases == {
        "sa.compiler::SQLCompiler": ["sa.compiler::Compiled"],
        "sa.mssql::MSCompiler": ["sa.compiler::SQLCompiler"],
    }
    pf = parsed["sa/mssql.py"]
    pf.includes = [p for p in resolve_imports(pf.path, list(pf.includes), present) if p != pf.path]
    ctx = {"present": present, "py_present": present, "py_class_bases": class_bases}
    edges, _ = extract_edges_and_misses(pf, gated, None, None, ctx)
    calls = {
        (e.line, e.to_qualified_name, round(e.confidence, 2))
        for e in edges
        if e.edge_kind == "calls"
    }
    assert any(line == 6 and q == "sa.compiler::Compiled::process" for line, q, _ in calls), (
        "self.process() through SQLCompiler's row to Compiled"
    )
    assert not any(line == 9 for line, _q, _ in calls), "`object.__getattribute__` is the builtin's"
    without = extract_edges_and_misses(
        pf, gated, None, None, {"present": present, "py_present": present}
    )[0]
    assert not any(e.line == 6 for e in without if e.edge_kind == "calls"), (
        "without the rows the walk stops at the imported base and `process` is ambiguous"
    )


DOTTED = {
    "web/db/__init__.py": b"from web.db.base import *\n",
    "web/db/base.py": b"class Model:\n    def save(self):\n        return 1\n",
    "web/sql/expression.py": b"from web.sql.elements import and_ as and_\n",
    "web/sql/elements.py": b"def and_(*c):\n    return c\n\n\nclass Op:\n    def and_(self, o):\n        return o\n",
    "app/models.py": (
        b"from web import db as models\nfrom web.sql.expression import and_\n\n\nclass Article(models.Model):\n"
        b"    def save(self):\n        return super().save()\n\n\ndef q():\n    return and_(1, 2)\n"
    ),
}


def test_a_dotted_base_and_a_plain_module_reexport_resolve():
    present, parsed, name_table, _ = _corpus(DOTTED)

    gated = gate_name_table(name_table, "python")
    pf = parsed["app/models.py"]
    pf.includes = [p for p in resolve_imports(pf.path, list(pf.includes), present) if p != pf.path]
    texts = {p: src.decode() for p, src in DOTTED.items()}
    ctx = {"present": present, "py_present": present, "py_init_texts": texts}
    edges = extract_edges_and_misses(pf, gated, None, None, ctx)[0]
    kinds = {(e.edge_kind, e.line, e.to_qualified_name) for e in edges}
    assert ("inherits", 5, "web.db.base::Model") in kinds, (
        "`models.Model`: the module alias, then the star re-export"
    )
    assert ("calls", 7, "web.db.base::Model::save") in kinds, "super().save() through that base"
    assert ("calls", 11, "web.sql.elements::and_") in kinds, (
        "a plain module's `import ... as` re-export"
    )
