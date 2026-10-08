"""A hypothetical answer votes — a second, capped vector list from a model-written passage,
kind-gated; off by default; a unit only it found says via: hyde."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from server.code.app import search as cs
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


class _Repo:
    def __init__(self) -> None:
        self.vector_calls: list[list[float]] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [{"chunk_id": "L1", "path": "src/a.cpp", "lex_score": 4.0, **_UNIT}]

    async def vector_search_chunks(self, org, ns, model, vec, **k):
        self.vector_calls.append(list(vec))
        if vec[0] == 9.0:  # the passage's vector
            return [
                {"chunk_id": "H1", "path": "src/Common/MemoryTracker.cpp", "distance": 0.2, **_UNIT}
            ]
        return [{"chunk_id": "V1", "path": "src/b.cpp", "distance": 0.3, **_UNIT}]

    async def vector_search_cards(self, *a, **k):
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [9.0, 0.0, 0.0, 0.0] if "MemoryTracker" in text else [0.5, 0.5, 0.5, 0.5]


class _LLM:
    model = "gpt-4o"
    calls = 0

    async def complete(self, system, user, max_tokens=220):
        _LLM.calls += 1

        class R:
            text = "DB::MemoryTracker::allocImpl in src/Common/MemoryTracker.cpp enforces the per-query memory limit."

        return R()


def _run(query: str, model: str, kinds: str = "responsibility"):
    repo = _Repo()
    _LLM.calls = 0
    with (
        patch.multiple(
            settings,
            code_fusion_signals=False,
            code_search_card_leg=False,
            code_search_concept_expand_model="",
            code_search_hyde_model=model,
            code_search_hyde_kinds=kinds,
            code_search_hyde_cap=1.0,
        ),
        patch("server.llm.client.get_llm_client_for_model", lambda m: _LLM()),
    ):
        res = asyncio.run(cs.search(repo, "o", "n", query=query, top_k=5, embedder=_Embedder()))
    return repo, res


def test_hyde_leg_off_by_default() -> None:
    repo, res = _run("where is the memory limit for a query enforced", "")
    assert _LLM.calls == 0 and len(repo.vector_calls) == 1
    assert "hyde_queries" not in res.retrieval_work and all(u.via != "hyde" for u in res.units)


def test_hyde_leg_votes_for_a_unit_the_question_missed() -> None:
    repo, res = _run("where is the memory limit for a query enforced", "gpt-4o")
    assert (
        _LLM.calls == 1
        and len(repo.vector_calls) == 2
        and any(v[0] == 9.0 for v in repo.vector_calls)
    )
    unit = next(u for u in res.units if u.unit_id == "H1")
    assert (
        unit.via == "hyde"
        and res.retrieval_work["hyde_queries"] == 1
        and res.retrieval_work["hyde_chars"] > 20
    )


def test_hyde_leg_is_kind_gated() -> None:
    repo, res = _run("Segfault in MergeTreeData::loadDataParts after upgrade to 25.3", "gpt-4o")
    assert _LLM.calls == 0 and "hyde_queries" not in res.retrieval_work
