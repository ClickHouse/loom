"""The lexical list can weigh less than the vector list in
the reciprocal-rank fusion, and the weight applies to responsibility
questions only."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.code.app import search as cs
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


def test_lexical_weight_scales_only_the_lexical_list() -> None:
    lexical = [_row("L1"), _row("X")]
    vector = [_row("V1"), _row("X")]
    equal = dict((r["chunk_id"], s) for r, s in _rrf_fuse(lexical, vector))
    half = dict((r["chunk_id"], s) for r, s in _rrf_fuse(lexical, vector, lexical_weight=0.5))
    assert equal["L1"] == equal["V1"] == 1.0 / (_RRF_K + 1)
    assert half["L1"] == 0.5 / (_RRF_K + 1) and half["V1"] == 1.0 / (_RRF_K + 1)
    assert half["X"] == 0.5 / (_RRF_K + 2) + 1.0 / (_RRF_K + 2)
    # at 0.5 a vector-only top beats a lexical-only top
    assert half["V1"] > half["L1"] and equal["V1"] == equal["L1"]


class _Repo:
    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [_row("L1", "src/lex.cpp", lex_score=40.0)]

    async def vector_search_chunks(self, *a, **k):
        return [_row("V1", "src/vec.cpp", distance=0.3)]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(query: str):
    with patch.multiple(
        settings,
        code_fusion_signals=False,
        code_search_card_leg=False,
        code_search_abstain_card_below=0.0,
        code_search_concept_expand_model="",
        code_search_rrf_lexical_weight_responsibility=0.5,
    ):
        return asyncio.run(cs.search(_Repo(), "o", "n", query=query, top_k=5, embedder=_Embedder()))


def test_weight_applies_to_responsibility_questions_only() -> None:
    res = _run("how does the server enforce the memory limit")
    assert res.units[0].path == "src/vec.cpp" and "rrf(lex_w=0.5)" in res.score_explanation
    res = _run("Fix crash in sequenceMatch with time gates")
    assert "lex_w" not in res.score_explanation
