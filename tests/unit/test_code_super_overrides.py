"""`super.m()` binds to the bound base's method and a
derived method that redefines a base method carries an `overrides` edge."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses

TABLE = {
    "Base": [("b1", "src.base::Base", "class")],
    "run": [("br", "src.base::Base::run", "function")],
    "stop": [("bs", "src.base::Base::stop", "function")],
}
SRC = (
    b"import { Base } from './base'\nimport { Component } from 'react'\n"
    b"export class Derived extends Base {\n  run() {\n    super.run()\n    return this.helper()\n  }\n"
    b"  stop() {}\n  helper() { return 1 }\n}\n"
    b"export class Other extends Component {\n  render() { super.render(); return null }\n}\n"
    b"export class Local extends Derived {\n  run() { return super.run() }\n}\n"
)


def test_super_binds_to_the_bound_base_and_declines_a_library_base() -> None:
    pf = parse_file("src/derived.ts", SRC)
    edges, misses = extract_edges_and_misses(pf, TABLE)
    calls = [
        (e.from_qualified_name.split("::", 1)[1], e.to_qualified_name, e.confidence)
        for e in edges
        if e.edge_kind == "calls"
    ]
    assert ("Derived::run", "src.base::Base::run", 1.0) in calls
    assert ("Derived::run", "src.derived::Derived::helper", 1.0) in calls  # same-file unique rung
    # Local extends the same-file Derived: super.run() is Derived::run, not Base::run
    assert ("Local::run", "src.derived::Derived::run", 1.0) in calls
    # Other extends a library class: super.render() is neither an edge nor a miss
    assert not any(e.to_qualified_name.endswith("::render") for e in edges)
    assert [m.callee_name for m in misses] == []


def test_derived_methods_override_the_base_methods_they_redefine() -> None:
    pf = parse_file("src/derived.ts", SRC)
    edges, _ = extract_edges_and_misses(pf, TABLE)
    over = sorted(
        (e.from_qualified_name.split("::", 1)[1], e.to_qualified_name, e.line)
        for e in edges
        if e.edge_kind == "overrides"
    )
    assert over == [
        ("Derived::run", "src.base::Base::run", 4),
        ("Derived::stop", "src.base::Base::stop", 8),
        ("Local::run", "src.derived::Derived::run", 15),
    ]


def test_a_decorator_or_field_initializer_call_belongs_to_the_class() -> None:
    pf = parse_file(
        "src/dto.ts",
        b"import { Roles } from './roles'\nimport { makeQueue } from './q'\n"
        b"@Roles('admin')\nexport class Dto {\n  private queue = makeQueue()\n  run() { return 1 }\n}\n",
    )
    table = {
        "Roles": [("r1", "src.roles::Roles", "function")],
        "makeQueue": [("q1", "src.q::makeQueue", "function")],
    }
    edges, misses = extract_edges_and_misses(pf, table)
    calls = sorted(
        (e.from_qualified_name, e.to_qualified_name) for e in edges if e.edge_kind == "calls"
    )
    assert calls == [("src.dto::Dto", "src.q::makeQueue"), ("src.dto::Dto", "src.roles::Roles")]
    assert misses == []
