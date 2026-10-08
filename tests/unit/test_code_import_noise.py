"""A noise-named call the file imports from a repository
module is a call about that module's function, and a member read through a
namespace import binds to the module's export."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses
from server.code.domain.extract.lang_typescript_imports import import_bindings
from server.code.domain.extract.lang_typescript_refs import member_read_edges, member_read_sites

TABLE = {
    "parse": [("p1", "src.parse::parse", "function")],
    "VERSION": [("v1", "src.utils::VERSION", "const")],
}


def _calls(path: str, src: bytes) -> list[tuple[str, float]]:
    edges, _ = extract_edges_and_misses(parse_file(path, src), TABLE)
    return [(e.to_qualified_name, e.confidence) for e in edges if e.edge_kind == "calls"]


def test_an_imported_noise_name_binds_to_the_repository_module() -> None:
    assert _calls(
        "src/a.ts",
        b"import { parse } from './parse'\nexport function run(x: string) {\n  return parse(x)\n}\n",
    ) == [
        ("src.parse::parse", 1.0),
    ]
    assert _calls(
        "src/b.ts",
        b"import * as p from './parse'\nexport function run(x: string) {\n  return p.parse(x)\n}\n",
    ) == [
        ("src.parse::parse", 1.0),
    ]


def test_a_library_or_bare_noise_name_stays_dropped() -> None:
    assert (
        _calls(
            "src/c.ts",
            b"import { parse } from 'zod'\nexport function run(x: string) {\n  return parse(x)\n}\n",
        )
        == []
    )
    assert (
        _calls("src/d.ts", b"export function run(x: string) {\n  return JSON.parse(x)\n}\n") == []
    )


def test_a_member_read_through_a_namespace_import_binds_to_the_export() -> None:
    pf = parse_file(
        "src/e.ts",
        b"import * as utils from './utils'\nexport function run() {\n  return utils.VERSION\n}\n",
    )
    b = import_bindings(pf)
    edges = member_read_edges(pf, member_read_sites(pf, set(b)), TABLE, b)
    assert [(e.edge_kind, e.to_qualified_name, e.confidence) for e in edges] == [
        ("reads", "src.utils::VERSION", 1.0)
    ]
    lib = parse_file(
        "src/f.ts", b"import * as z from 'zod'\nexport function run() {\n  return z.VERSION\n}\n"
    )
    assert member_read_edges(lib, member_read_sites(lib, {"z"}), TABLE, import_bindings(lib)) == []


def test_a_bare_package_import_keeps_its_chain_verbs_as_noise() -> None:
    from server.code.domain.extract.edges_treesitter import collect_call_names

    pf = parse_file(
        "src/g.ts",
        b"import { z } from 'zod'\nimport * as shared from '@langfuse/shared'\nexport const S = z.object({ a: z.string() })\nexport const r = () => shared.parse('x')\n",
    )
    names = collect_call_names(pf)
    assert "object" not in names and "string" not in names
    assert "parse" in names
