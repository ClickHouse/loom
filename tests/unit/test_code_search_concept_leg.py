"""A prose question is mapped by a language model to the
identifiers the code likely uses; a lexical pass on those terms joins the
fusion as a capped list, a unit only it found says `via: concept`, and the
leg is off without a model and for symbol-shaped queries."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.app.search.cards import parse_concept_terms
from server.code.app.search.fusion import _RRF_K, _rrf_fuse
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


def test_parse_concept_terms_is_tolerant_and_deduplicates() -> None:
    text = (
        'Sure:\n```json\n{"identifiers": ["MemoryTracker", "ProcessList", "memorytracker"], '
        '"terms": ["memory limit", "max_memory_usage", ""]}\n```'
    )
    assert parse_concept_terms(text) == [
        "MemoryTracker",
        "ProcessList",
        "memory limit",
        "max_memory_usage",
    ]
    assert parse_concept_terms(text, max_terms=2) == ["MemoryTracker", "ProcessList"]
    assert parse_concept_terms("not json") == [] and parse_concept_terms('["a"]') == []


def test_concept_rows_are_a_capped_list_in_the_fusion() -> None:
    lexical = [_row("L1"), _row("L2")]
    vector = [_row("V1"), _row("L1")]
    concept = [_row("C1", "src/c.cpp"), _row("L2")]
    fused = dict(
        (r["chunk_id"], s)
        for r, s in _rrf_fuse(
            lexical,
            vector,
            concept_rows=concept,
            concept_cap=0.5,
        )
    )
    top = 1.0 / (_RRF_K + 1) + 1.0 / (_RRF_K + 2)  # L1: lexical rank 1, vector rank 2
    assert fused["L1"] == top
    assert fused["C1"] == min(1.0 / (_RRF_K + 1), 0.5 * top)
    assert fused["L2"] == 1.0 / (_RRF_K + 2) + min(1.0 / (_RRF_K + 2), 0.5 * top)


class _Repo:
    def __init__(self) -> None:
        self.lexical_calls: list[list[str]] = []

    async def code_corpus_stats(self, org, ns, tokens):
        # "memory" is in most chunks: its idf falls under the floor and the
        # concept pass drops it
        return {t: (95 if t == "memory" else 3) for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, org, ns, model, tokens, idfs, **k):
        self.lexical_calls.append(list(tokens))
        if any(t.lower().startswith("memorytracker") for t in tokens):
            return [_row("CONCEPT", "src/Common/MemoryTracker.cpp", lex_score=9.0)]
        return [_row("a", lex_score=40.0), _row("b", lex_score=10.0)]

    async def vector_search_chunks(self, *a, **k):
        return [_row("c", distance=0.3), _row("a", distance=0.35)]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def symbols_by_name(self, *a, **k):
        return []

    async def scope_file_count(self, *a, **k):
        return 4

    async def unit_signals(self, *a, **k):
        return {}


class _LLM:
    model = "gpt-4o-mini"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.calls += 1
        assert "code-clickhouse" in system and response_format == {"type": "json_object"}
        return type(
            "R",
            (),
            {
                "text": '{"identifiers": ["MemoryTracker", "ProcessList"], "terms": ["memory limit"]}'
            },
        )()


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(repo, llm, query="where is the memory limit for a query enforced", **settings_patch):
    patches = {
        "code_fusion_signals": False,
        "code_search_card_leg": False,
        "code_search_concept_expand_model": "gpt-4o-mini",
        **settings_patch,
    }
    with (
        patch.multiple(settings, **patches),
        patch("server.llm.client.get_llm_client_for_model", lambda m: llm),
    ):
        return asyncio.run(
            cs.search(repo, "o", "code-clickhouse", query=query, top_k=5, embedder=_Embedder())
        )


def test_concept_leg_runs_a_second_lexical_pass_and_labels_its_units() -> None:
    repo, llm = _Repo(), _LLM()
    res = _run(repo, llm)
    assert llm.calls == 1 and len(repo.lexical_calls) == 2
    assert any("memorytracker" in t.lower() for t in repo.lexical_calls[1])
    assert (
        "memory" not in repo.lexical_calls[1] and res.retrieval_work["concept_tokens_dropped"] >= 1
    )
    unit = next(u for u in res.units if u.path.endswith("MemoryTracker.cpp"))
    assert unit.via == "concept"
    assert (
        res.retrieval_work["concept_terms"] == 3
        and "concept=MemoryTracker|ProcessList|memory limit" in res.score_explanation
    )
    # the content legs' top stays on top: the concept list is capped at the two-leg top
    assert res.units[0].path == "src/a.cpp"


def test_concept_leg_is_off_without_a_model_and_for_symbol_shapes() -> None:
    repo, llm = _Repo(), _LLM()
    _run(repo, llm, code_search_concept_expand_model="")
    assert llm.calls == 0 and len(repo.lexical_calls) == 1
    repo, llm = _Repo(), _LLM()
    _run(repo, llm, query="MemoryTracker::allocImpl")
    assert llm.calls == 0
