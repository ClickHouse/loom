"""A symbol answers with the document sections that cite it —
the Markdown lane's `reads` edges, read once on the definitions' ids; a SQL
statement's table read is not a document; five distinct sections at most."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.api.models_symbol import CodeSymbolOutput
from server.code.app.symbol import documented_in_for, is_doc_path

ROWS = [
    {"symbol_id": "a", "qualified_name": "DB::Foo::bar"},
    {"symbol_id": "a2", "qualified_name": "DB::Foo::bar"},
]  # both tiers' ids


def _edge(frm: str, to: str, path: str, line: int, conf: float = 0.7) -> dict[str, Any]:
    return {
        "edge_kind": "reads",
        "from_symbol_id": frm.replace("::", "_"),
        "to_symbol_id": to,
        "path": path,
        "line": line,
        "tier": "treesitter",
        "confidence": conf,
        "from_qualified_name": frm,
        "to_qualified_name": "DB::Foo::bar",
    }


class _Repo:
    def __init__(self, edges: list[dict[str, Any]]) -> None:
        self.edges = edges
        self.calls: list[dict[str, Any]] = []

    async def edges_for_symbols(
        self,
        org,
        ns,
        ids,
        *,
        direction,
        edge_kinds=None,
        limit=200,
        min_confidence=0.0,
        per_match=None,
    ):
        self.calls.append(
            {
                "ids": list(ids),
                "direction": direction,
                "kinds": edge_kinds,
                "min_confidence": min_confidence,
            }
        )
        return [e for e in self.edges if e["to_symbol_id"] in ids]


def test_sections_distinct_in_read_order_documents_only_capped_at_five() -> None:
    edges = [
        _edge("docs.design::Design::Commit path", "a", "docs/design.md", 40),
        _edge(
            "docs.design::Design::Commit path", "a2", "docs/design.md", 41
        ),  # same section, other tier
        _edge("tests.q::stmt#1", "a", "tests/q.sql", 3, 0.9),  # a table read, not a document
        *[_edge(f"docs.more::S{i}", "a", "docs/more.md", i) for i in range(6)],
    ]
    repo = _Repo(edges)
    out = asyncio.run(documented_in_for(repo, "o", "n", ROWS))
    assert [d["section"] for d in out] == [
        "docs.design::Design::Commit path",
        "docs.more::S0",
        "docs.more::S1",
        "docs.more::S2",
        "docs.more::S3",
    ]
    assert out[0] == {
        "section": "docs.design::Design::Commit path",
        "path": "docs/design.md",
        "line": 40,
    }
    assert repo.calls == [
        {"ids": ["a", "a2"], "direction": "callers", "kinds": ["reads"], "min_confidence": 0.6}
    ], "one read, both tiers' ids, reads edges only"


def test_no_rows_no_read_and_the_field_defaults_empty() -> None:
    repo = _Repo([])
    assert asyncio.run(documented_in_for(repo, "o", "n", [])) == [] and repo.calls == []
    assert CodeSymbolOutput(definitions=[]).documented_in == []


def test_document_paths() -> None:
    assert is_doc_path("docs/a.md") and is_doc_path("README.MD") and is_doc_path("x/y.rst")
    assert not is_doc_path("tests/q.sql") and not is_doc_path("src/a.cpp")
