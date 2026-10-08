"""code.search fuses its legs by rank (RRF, k=60): the fused order and the
explanation label come from `_rrf_fuse`."""

from __future__ import annotations

from server.code.app.search.fusion import _RRF_K, _rrf_fuse
from server.settings import settings


def _lex(cid: str, score: float) -> dict:
    return {"chunk_id": cid, "lex_score": score, "path": f"src/{cid}.cpp"}


def _vec(cid: str, distance: float) -> dict:
    return {"chunk_id": cid, "distance": distance, "path": f"src/{cid}.cpp"}


def test_rank_agreement_beats_a_single_leg_score_gap() -> None:
    # a leads the lexical leg by 4x; b is rank 2 lexical + rank 1 vector and wins on ranks
    lexical = [_lex("a", 40.0), _lex("b", 10.0), _lex("c", 9.0)]
    vector = [_vec("b", 0.30), _vec("c", 0.31), _vec("a", 0.60)]
    fused = _rrf_fuse(lexical, vector)
    assert [r["chunk_id"] for r, _ in fused] == ["b", "a", "c"]
    scores = {r["chunk_id"]: s for r, s in fused}
    assert scores["b"] == 1.0 / (_RRF_K + 2) + 1.0 / (_RRF_K + 1)


def test_search_explains_the_rank_fusion() -> None:
    """search() fuses by rank; the explanation opens with the fusion label."""
    import asyncio
    from unittest.mock import patch

    from server.code.app import search as cs

    lexical = [_lex("a", 40.0), _lex("b", 10.0)]

    class _Repo:
        async def code_corpus_stats(self, org, ns, tokens):
            return {}, 100, 56.0

        async def lexical_search_chunks(self, *a, **k):
            return [
                dict(
                    r,
                    path="src/x.cpp",
                    scope="f",
                    kind="function",
                    start_line=1,
                    end_line=5,
                    content="x",
                    symbol_ids=[],
                    context_description="",
                )
                for r in lexical
            ]

        async def symbols_fuzzy(self, *a, **k):
            return []

        async def unit_signals(self, *a, **k):
            return {}

    with patch.object(settings, "code_fusion_signals", False):
        res = asyncio.run(
            cs.search(
                _Repo(),
                "o",
                "n",
                query="where do we retry uploads",
                top_k=2,
            )
        )
    assert res.score_explanation.startswith("rrf")
