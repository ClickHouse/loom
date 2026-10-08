"""Heritage clauses bind on the call ladder;
a type's hierarchy rides code.symbol; a callers walk can be restricted to
edge kinds; the gold's heritage parser."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import (
    IMPORT_BOUND_CONFIDENCE,
    extract_edges_and_misses,
)
from server.core.errors import ValidationError


def _table(*rows: tuple[str, str, str]) -> dict[str, list[tuple[str, str, str]]]:
    out: dict[str, list[tuple[str, str, str]]] = {}
    for sid, qn, kind in rows:
        out.setdefault(qn.split("::")[-1], []).append((sid, qn, kind))
    return out


def _inherits(edges: Any) -> dict[str, tuple[str, float]]:
    return {
        e.from_qualified_name: (e.to_qualified_name, round(e.confidence, 2))
        for e in edges
        if e.edge_kind == "inherits"
    }


def test_a_same_file_base_binds_at_one_even_when_the_repo_has_namesakes() -> None:
    # fastify's test tree: three files each declare a LogController and a
    # subclass of it — repo-wide the name is ambiguous, in the file it is not
    src = b"""class LogController { emit() {} }
class QuietLog extends LogController { emit() {} }
"""
    pf = parse_file("test/logger/a.test.js", src)
    table = _table(
        ("lc1", "test.logger.a.test::LogController", "class"),
        ("lc2", "test.logger.b.test::LogController", "class"),
        ("lc3", "test.logger.c.test::LogController", "class"),
    )
    edges, _ = extract_edges_and_misses(pf, table, None)
    assert _inherits(edges) == {
        "test.logger.a.test::QuietLog": ("test.logger.a.test::LogController", 1.0)
    }


def test_an_imported_base_binds_through_its_import_and_a_library_base_binds_nothing() -> None:
    # `implements Filter` is ambiguous by name alone (a `Filter` interface and a
    # `Filter` type elsewhere); the import names the module.
    # `extends Error` and a plugin interface imported from a library must
    # never bind to a repository namesake.
    src = b"""import { Filter } from './filter';
import { KyselyPlugin } from 'kysely';
export class StringFilter implements Filter { apply() {} }
export class Camel implements KyselyPlugin { apply() {} }
export class NotFound extends Error {}
"""
    pf = parse_file("packages/shared/src/queries/string.ts", src)
    pf.includes = ["packages/shared/src/queries/filter.ts"]  # kysely resolved to no module
    table = _table(
        ("f1", "packages.shared.src.queries.filter::Filter", "interface"),
        ("f2", "web.src.features.filters.types::Filter", "type"),
        ("k1", "packages.shared.src.vendor.plugin::KyselyPlugin", "interface"),
        ("e1", "packages.shared.src.errors::Error", "class"),
    )
    edges, _ = extract_edges_and_misses(pf, table, None)
    assert _inherits(edges) == {
        "packages.shared.src.queries.string::StringFilter": (
            "packages.shared.src.queries.filter::Filter",
            IMPORT_BOUND_CONFIDENCE,
        ),
    }


def test_a_repo_unique_base_still_binds_without_an_import() -> None:
    src = b"""export class Runner extends PeriodicRunner { tick() {} }
"""
    pf = parse_file("worker/src/runner.ts", src)
    table = _table(("p1", "worker.src.periodic::PeriodicRunner", "class"))
    edges, _ = extract_edges_and_misses(pf, table, None)
    assert _inherits(edges) == {
        "worker.src.runner::Runner": ("worker.src.periodic::PeriodicRunner", 0.9)
    }


# ---------------------------------------------------------------- reads


class _Repo:
    def __init__(self, ups: list[dict], downs: list[dict], symbols: list[dict]) -> None:
        self.ups, self.downs, self.symbols = ups, downs, symbols
        self.asked: list[tuple[str, list[str] | None]] = []

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200, **kw):
        self.asked.append((direction, edge_kinds))
        return list(self.ups if direction == "callees" else self.downs)

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols if s["symbol_id"] in ids]


def _edge(frm: str, to: str, tier: str, fq: str, tq: str, conf: float = 1.0) -> dict:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "tier": tier,
        "edge_kind": "inherits",
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "confidence": conf,
        "path": "src/x.ts",
        "line": 3,
    }


def test_a_type_seed_carries_its_supertypes_and_subtypes_folded_across_tiers() -> None:
    from server.code.app.symbol.hierarchy import _symbol_hierarchy

    seeds = [
        {"symbol_id": "s", "kind": "class", "qualified_name": "m::Base", "tier": "scip"},
        {"symbol_id": "t", "kind": "class", "qualified_name": "m::Base", "tier": "treesitter"},
    ]
    ups = [
        _edge("s", "i", "scip", "m::Base", "m::Shape"),
        _edge("t", "i2", "treesitter", "m::Base", "m::Shape", 0.9),
    ]  # the same interface, other tier
    downs = [
        _edge("d", "s", "scip", "m::Circle", "m::Base"),
        _edge("d2", "t", "treesitter", "m::Circle", "m::Base", 1.0),
        _edge("e", "t", "treesitter", "m::Square", "m::Base", 0.9),
    ]
    symbols = [
        {"symbol_id": "i", "kind": "interface"},
        {"symbol_id": "i2", "kind": "interface"},
        {"symbol_id": "d", "kind": "class"},
        {"symbol_id": "d2", "kind": "class"},
        {"symbol_id": "e", "kind": "class"},
    ]
    repo = _Repo(ups, downs, symbols)
    out = asyncio.run(_symbol_hierarchy(repo, "o", "n", seeds))  # type: ignore[arg-type]
    assert [(r["symbol"], r["via"], r["tier"]) for r in out["supertypes"]] == [
        ("m::Shape", "implements", "scip"),
    ]
    assert [(r["symbol"], r["via"], r["tier"], r["confidence"]) for r in out["subtypes"]] == [
        ("m::Circle", "extends", "scip", 1.0),
        ("m::Square", "extends", "treesitter", 0.9),
    ]
    assert repo.asked == [("callees", ["inherits"]), ("callers", ["inherits"])]


def test_a_function_seed_reads_no_hierarchy() -> None:
    from server.code.app.symbol.hierarchy import _symbol_hierarchy

    repo = _Repo([], [], [])
    out = asyncio.run(_symbol_hierarchy(repo, "o", "n", [{"symbol_id": "f", "kind": "function"}]))  # type: ignore[arg-type]
    assert out == {} and repo.asked == []


def test_interface_extending_interface_is_extends() -> None:
    from server.code.app.symbol.hierarchy import _heritage_via

    assert _heritage_via("class", "interface") == "implements"
    assert _heritage_via("interface", "interface") == "extends"
    assert _heritage_via("class", "class") == "extends"


def test_edge_kinds_narrow_the_walk_and_an_unknown_kind_is_a_400() -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol.edges import _GRAPH_EDGE_KINDS, _walk_kinds

    assert _walk_kinds(CodeCallersInput(name="X")) == _GRAPH_EDGE_KINDS
    assert _walk_kinds(CodeCallersInput(name="X", edge_kinds=["inherits"])) == ["inherits"]
    with pytest.raises(ValidationError):
        _walk_kinds(CodeCallersInput(name="X", edge_kinds=["mentions"]))


def test_an_empty_hierarchy_is_not_on_the_wire() -> None:
    from server.code.api.models_symbol import CodeSymbolOutput

    assert "hierarchy" not in CodeSymbolOutput(definitions=[]).model_dump()
    dumped = CodeSymbolOutput(
        definitions=[], hierarchy={"subtypes": [{"symbol": "m::A"}]}
    ).model_dump()
    assert dumped["hierarchy"] == {"subtypes": [{"symbol": "m::A"}]}


# ---------------------------------------------------------------- gold


def test_the_gold_reads_heritage_clauses_and_skips_library_bases() -> None:
    from bench.gen_code_intel_qa_langfuse import _class_heads, _heritage_bases

    text = (
        "export abstract class Base<\n  T extends Record<string, string>,\n>\n  extends Parent<T>\n"
        "  implements Filter, Runnable<T> {\n}\n"
        "class Boom extends React.Component {}\n"
        "class Oops extends Error {}\n"
        "export class Plain {}\n"
        "export default class Job extends ChunkedMigration<PartChunkTodo> {\n}\n"
    )
    found = [(n, _heritage_bases(c)) for n, c in _class_heads(text)]
    assert found == [
        ("Base", ["Parent", "Filter", "Runnable"]),
        ("Boom", []),
        ("Oops", []),
        ("Plain", []),
        ("Job", ["ChunkedMigration"]),
    ]
