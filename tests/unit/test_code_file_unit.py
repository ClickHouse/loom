"""A file is the unit of its own top-level code — calls at
module level and inside anonymous callbacks are edges from the file, with
the id the `includes` edges use; a C++ file gets no such unit."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.treesitter import file_symbol_id, file_unit

TABLE = {
    "doThing": [("d1", "src.lib::doThing", "function")],
    "other": [("o1", "src.lib::other", "function")],
    "do_thing": [("p1", "lib::do_thing", "function")],
}


def test_top_level_and_callback_calls_are_edges_from_the_file() -> None:
    pf = parse_file(
        "src/routes.ts",
        b"import { doThing, other } from './lib'\nconst app = express()\n"
        b"app.get('/x', async (req, res) => {\n  await doThing(req)\n})\nother()\nexport default app\n",
    )
    edges, misses = extract_edges_and_misses(pf, TABLE)
    calls = [
        (e.from_symbol_id, e.from_qualified_name, e.to_qualified_name, e.line)
        for e in edges
        if e.edge_kind == "calls"
    ]
    fid = file_symbol_id("src/routes.ts")
    assert calls == [
        (fid, "src/routes.ts", "src.lib::doThing", 4),
        (fid, "src/routes.ts", "src.lib::other", 6),
    ]
    # `express()` is a library call with no candidates: a miss under the file
    assert [(m.callee_name, m.line) for m in misses] == [("express", 2)]
    unit = file_unit(pf)
    assert unit is not None and unit.id == fid and unit.kind == "file" and unit.name == "routes.ts"


def test_a_function_body_still_owns_its_calls() -> None:
    pf = parse_file(
        "src/a.ts", b"import { doThing } from './lib'\nexport function run() {\n  doThing()\n}\n"
    )
    edges, _ = extract_edges_and_misses(pf, TABLE)
    assert [
        (e.from_qualified_name, e.to_qualified_name) for e in edges if e.edge_kind == "calls"
    ] == [
        ("src.a::run", "src.lib::doThing"),
    ]


def test_python_module_level_calls_and_cpp_have_no_file_unit() -> None:
    py = parse_file(
        "app/main.py",
        b"from lib import do_thing\napp = do_thing(1)\nif __name__ == '__main__':\n    do_thing(2)\n",
    )
    edges, _ = extract_edges_and_misses(py, TABLE)
    assert [
        (e.from_qualified_name, e.to_qualified_name, e.line)
        for e in edges
        if e.edge_kind == "calls"
    ] == [
        ("app/main.py", "lib::do_thing", 2),
        ("app/main.py", "lib::do_thing", 4),
    ]
    cpp = parse_file("src/x.cpp", b"int f() { return 1; }\n")
    assert file_unit(cpp) is None
