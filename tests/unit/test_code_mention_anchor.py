"""A prose code.search query that NAMES a symbol is ranked by fusion alone.

There is no mention-anchoring step that lifts the named definition above the
fused list: no symbol lookup, route `hybrid`."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.settings import settings


def _sym(name: str, path: str, sid: str, tier: str = "scip") -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "path": path,
        "start_line": 1,
        "end_line": 9,
        "tier": tier,
        "signature": "()",
        "doc_comment": "d",
    }


class _Repo:
    def __init__(self) -> None:
        self.by_name: dict[str, list[dict[str, Any]]] = {
            "PipelineExecutor": [
                _sym("DB::PipelineExecutor", "src/Processors/Executors/PipelineExecutor.h", "s1"),
                _sym(
                    "DB::PipelineExecutor::PipelineExecutor",
                    "src/Processors/Executors/PipelineExecutor.cpp",
                    "sctor",
                ),
                _sym(
                    "DB::PipelineExecutor",
                    "src/Processors/Executors/PipelineExecutor.h",
                    "s1t",
                    tier="treesitter",
                ),
            ],
            # a common name: five definitions
            "split_chunks": [
                _sym(f"X{i}::split_chunks", f"src/x{i}.cpp", f"c{i}") for i in range(5)
            ],
        }
        self.lookups: list[str] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [
            {
                "chunk_id": "k1",
                "score": 30.0,
                "path": "src/Processors/Executors/ExecutingGraph.cpp",
                "scope": "DB::ExecutingGraph::schedule",
                "kind": "function",
                "start_line": 1,
                "end_line": 5,
                "content": "x",
                "symbol_ids": [],
                "context_description": "",
            },
            {
                "chunk_id": "k2",
                "score": 10.0,
                "path": "src/Processors/Executors/PipelineExecutor.h",
                "scope": "DB::PipelineExecutor",
                "kind": "class_skeleton",
                "start_line": 1,
                "end_line": 9,
                "content": "y",
                "symbol_ids": [],
                "context_description": "",
            },
        ]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        self.lookups.append(name)
        return self.by_name.get(name, [])[:limit]


def _run(repo: _Repo, query: str, **kw: Any) -> cs.CodeSearchResult:
    with patch.object(settings, "code_fusion_signals", False):
        return asyncio.run(cs.search(repo, "o", "n", query=query, top_k=5, **kw))


def test_a_named_symbol_is_ranked_by_fusion_without_a_symbol_lookup() -> None:
    repo = _Repo()
    res = _run(repo, "how does PipelineExecutor schedule processors")
    assert repo.lookups == [] and res.route == "hybrid"
    assert [u.unit_id for u in res.units] == ["k1", "k2"]
