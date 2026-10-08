"""A deployment default for the rerank seam, gated by the
question's kind; the request's own endpoint wins; symbol-shaped searches and
constrained scopes are never reranked by default."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
from server.code.app.search import rerank as cs_rerank
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
    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [{"chunk_id": "L1", "path": "src/a.cpp", "lex_score": 4.0, **_UNIT}]

    async def vector_search_chunks(self, *a, **k):
        return [
            {"chunk_id": "V1", "path": "src/b.cpp", "distance": 0.3, **_UNIT},
            {"chunk_id": "V2", "path": "src/c.cpp", "distance": 0.4, **_UNIT},
        ]

    async def vector_search_cards(self, *a, **k):
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(query: str, default: str, kinds: str = "responsibility", **kw: Any):
    calls: list[tuple[str, int]] = []

    async def fake_rerank(endpoint, query, units, *, top=50, excerpt_chars=300):
        calls.append((endpoint, top))
        return list(reversed(units)), True

    with (
        patch.multiple(
            settings,
            code_fusion_signals=False,
            code_search_card_leg=False,
            code_search_concept_expand_model="",
            code_search_rerank_default=default,
            code_search_rerank_kinds=kinds,
            code_search_rerank_top=20,
        ),
        patch.object(cs_rerank, "_rerank_units", fake_rerank),
    ):
        res = asyncio.run(
            cs.search(_Repo(), "o", "n", query=query, top_k=5, embedder=_Embedder(), **kw)
        )
    return calls, res


def test_default_rerank_applies_to_the_named_kind_and_reports_itself() -> None:
    calls, res = _run(
        "how does the server sort data that does not fit in memory", "builtin:openai:gpt-4o-mini"
    )
    assert calls == [("builtin:openai:gpt-4o-mini", 20)]
    assert (
        res.retrieval_work.get("rerank_default") == 1
        and res.retrieval_work.get("inline_rerank_used") == 1
    )
    assert res.score_explanation_extra == "rerank=builtin"


def test_no_default_for_other_kinds_or_when_unset() -> None:
    calls, res = _run(
        "Segfault in MergeTreeData::loadDataParts after upgrade to 25.3",
        "builtin:openai:gpt-4o-mini",
    )
    assert calls == [] and "rerank_default" not in res.retrieval_work
    calls, res = _run("how does the server sort data that does not fit in memory", "")
    assert calls == []


def test_request_endpoint_wins_over_the_default() -> None:
    calls, res = _run(
        "how does the server sort data that does not fit in memory",
        "builtin:openai:gpt-4o-mini",
        rerank_endpoint="builtin:cohere",
    )
    assert calls == [("builtin:cohere", 50)] and "rerank_default" not in res.retrieval_work


def test_none_turns_the_default_off() -> None:
    calls, res = _run(
        "how does the server sort data that does not fit in memory",
        "builtin:openai:gpt-4o-mini",
        rerank_endpoint="none",
    )
    assert calls == [] and "rerank_default" not in res.retrieval_work


def test_code_ask_hands_the_request_rerank_to_the_search_unchanged() -> None:
    """code.ask resolves its rerank the way code.search does: "" leaves the kind-gated
    deployment default to the rerank stage, "none" reaches it as the off switch."""
    from server.code.api.models_search import CodeAskInput
    from server.code.app.ask import _ask_search_kwargs

    def kw(rerank: str) -> Any:
        inp = CodeAskInput(org="o", namespace="n", question="q", rerank=rerank)
        return _ask_search_kwargs(inp, 0.3, 0.2)["rerank_endpoint"]

    assert kw("") == "" and kw("none") == "none" and kw("builtin:cohere") == "builtin:cohere"


def _stage(outcome: bool, extra: str) -> Any:
    from server.code.app.search.state import CodeSearchResult
    from server.code.domain.units import CodeUnit

    async def fake_rerank(endpoint, query, units, *, top=50, excerpt_chars=300):
        return units, outcome

    result = CodeSearchResult(score_explanation_extra=extra)
    unit = CodeUnit(unit_id="u", path="a.py", symbol="f", kind="function", score=1.0)
    with patch.object(cs_rerank, "_rerank_units", fake_rerank):
        asyncio.run(cs_rerank._rerank_stage("builtin:x", "q", [unit], {}, result, prose=True))
    return result.score_explanation_extra


def test_the_rerank_note_joins_the_explanation_and_only_when_it_reordered() -> None:
    assert _stage(True, "concept=Foo|Bar") == "concept=Foo|Bar, rerank=builtin"
    assert _stage(True, "") == "rerank=builtin"
    # the call failed open: the fused order was kept, so no rerank is claimed
    assert _stage(False, "concept=Foo") == "concept=Foo"
