"""The concept leg's vocabulary can come from the
repository itself — the symbols defined in the files of the nearest file
cards — instead of a language model. `cards[:n]` selects it; the terms are
types first, junk filtered; the leg runs after the card read and labels its
units `via: concept`."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.app.search.cards import card_term_files, select_card_terms
from server.settings import settings

_UNIT = dict(
    scope="f",
    kind="function",
    start_line=1,
    end_line=5,
    content="x",
    symbol_ids=[],
    context_description="",
)


def _row(cid: str, path: str = "src/a.cpp", **kw: Any) -> dict[str, Any]:
    return {"chunk_id": cid, "path": path, **_UNIT, **kw}


def test_card_term_files_parses_the_spec() -> None:
    assert card_term_files("cards") == 5 and card_term_files("cards:3") == 3
    assert card_term_files("gpt-4o-mini") == 0 and card_term_files("") == 0
    assert card_term_files("cards:0") == 1  # at least one file


def test_select_card_terms_prefers_types_and_drops_junk() -> None:
    names = [
        {"name": "MemoryTracker", "kind": "class"},
        {"name": "MemoryTracker", "kind": "class"},
        {"name": "amount", "kind": "class"},
        {"name": "hard_limit", "kind": "class"},
        {"name": "$anonymous_type_0", "kind": "class"},
        {"name": "alloc", "kind": "function"},
        {"name": "allocImpl", "kind": "function"},
        {"name": "SELECT 1", "kind": "statement"},
        {"name": "MemoryTrackerBlockerInThread", "kind": "struct"},
        {"name": "peak", "kind": "class"},
    ]
    terms = select_card_terms(names, max_terms=8)
    assert terms[0] == "MemoryTracker"  # a type, defined twice
    assert (
        "MemoryTrackerBlockerInThread" in terms and "hard_limit" in terms and "allocImpl" in terms
    )
    for junk in ("amount", "peak", "$anonymous_type_0", "SELECT 1", "alloc"):
        assert junk not in terms
    assert select_card_terms(names, max_terms=2) == [
        "MemoryTracker",
        "MemoryTrackerBlockerInThread",
    ]


class _Repo:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.lexical_tokens: list[list[str]] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, org, ns, model, tokens, idfs, **k):
        self.calls.append("lexical")
        self.lexical_tokens.append(list(tokens))
        if any("memorytracker" in t.lower() for t in tokens):
            return [_row("C1", "src/Common/MemoryTracker.cpp", lex_score=9.0)]
        return [_row("a", lex_score=40.0), _row("b", lex_score=10.0)]

    async def vector_search_chunks(self, *a, **k):
        self.calls.append("vector")
        return [_row("c", distance=0.3), _row("a", distance=0.35)]

    async def vector_search_cards(
        self, org, ns, model, vec, *, path_prefix="", pool=15, skip_tests=False
    ):
        self.calls.append(f"cards:{pool}:{'src' if skip_tests else 'all'}")
        if skip_tests:
            return [
                {"path": "src/Common/MemoryTracker.h", "distance": 0.4},
                {"path": "src/Interpreters/ProcessList.cpp", "distance": 0.45},
            ]
        return [
            {"path": "tests/queries/0_stateless/00877_memory_limit.sql", "distance": 0.38},
            {"path": "src/Common/MemoryTracker.h", "distance": 0.4},
        ]

    async def symbol_names_for_paths(self, org, ns, paths, *, limit=200):
        self.calls.append("symbols:" + ",".join(paths))
        return [
            {"name": "MemoryTracker", "kind": "class", "path": paths[0]},
            {"name": "ProcessList", "kind": "class", "path": paths[-1]},
            {"name": "peak", "kind": "class", "path": paths[0]},
        ]

    async def chunks_for_paths(self, *a, **k):
        self.calls.append("chunks_for_paths")
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def symbols_by_name(self, *a, **k):
        return []

    async def scope_file_count(self, *a, **k):
        return 4

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(repo, **settings_patch):
    patches = {
        "code_fusion_signals": False,
        "code_search_card_leg": False,
        "code_search_abstain_card_below": 0.0,
        "code_search_concept_expand_model": "cards:2",
        **settings_patch,
    }
    with patch.multiple(settings, **patches):
        return asyncio.run(
            cs.search(
                repo,
                "o",
                "code-clickhouse",
                query="where is the memory limit for a query enforced",
                top_k=5,
                embedder=_Embedder(),
            )
        )


def test_cards_mode_expands_with_the_nearest_files_symbols_and_no_model() -> None:
    repo = _Repo()
    res = _run(repo)
    assert "cards:2:src" in repo.calls  # a source-only card read supplies the term files
    assert any(
        c == "symbols:src/Common/MemoryTracker.h,src/Interpreters/ProcessList.cpp"
        for c in repo.calls
    )
    assert res.retrieval_work["concept_term_files"] == 2
    assert "chunks_for_paths" not in repo.calls  # the ranking card leg stays off
    assert len(repo.lexical_tokens) == 2 and any(
        "memorytracker" in t.lower() for t in repo.lexical_tokens[1]
    )
    unit = next(u for u in res.units if u.path.endswith("MemoryTracker.cpp"))
    assert unit.via == "concept"
    assert res.retrieval_work["concept_terms"] == 2  # peak dropped
    assert "concept=MemoryTracker|ProcessList" in res.score_explanation
    assert all(isinstance(v, (int, str)) for v in res.retrieval_work.values())
