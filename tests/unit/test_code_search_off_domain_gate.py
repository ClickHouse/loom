"""A prose question whose nearest file card is far from it
abstains as `off_domain`. The gate reads three cards when the card leg is
off, never touches the fusion, and stays silent for symbol-shaped and
constrained searches and when the cards are close."""

from __future__ import annotations

import asyncio
from typing import Any
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


def _lex(cid: str, score: float) -> dict[str, Any]:
    return {"chunk_id": cid, "lex_score": score, "path": f"src/{cid}.cpp", **_UNIT}


def _vec(cid: str, distance: float) -> dict[str, Any]:
    return {"chunk_id": cid, "distance": distance, "path": f"src/{cid}.cpp", **_UNIT}


class _Repo:
    def __init__(self, card_distance: float) -> None:
        self.card_distance = card_distance
        self.calls: list[tuple[str, dict]] = []

    async def code_corpus_stats(self, org, ns, tokens):
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a, **k):
        return [_lex("a", 40.0), _lex("b", 10.0)]

    async def vector_search_chunks(self, *a, **k):
        return [_vec("c", 0.3), _vec("a", 0.35)]

    async def vector_search_cards(self, *a, **k):
        self.calls.append(("cards", k))
        return [
            {"path": "src/far.cpp", "distance": self.card_distance},
            {"path": "src/far2.cpp", "distance": self.card_distance + 0.01},
        ]

    async def chunks_for_paths(self, *a, **k):
        self.calls.append(("chunks_for_paths", k))
        return [_vec("z", 0.9)]

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def symbols_by_name(self, *a, **k):
        return []  # symbol table miss → the identifier probe runs

    async def scope_file_count(self, *a, **k):
        return 4  # the identifier is present → hybrid legs run, shape stays "symbol"

    async def unit_signals(self, *a, **k):
        return {}


class _Embedder:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def _run(repo, query="how does the websocket server handle reconnects", **settings_patch):
    patches = {
        "code_fusion_signals": False,
        "code_search_card_leg": False,
        "code_search_abstain_card_below": 0.35,
        **settings_patch,
    }
    with patch.multiple(settings, **patches):
        return asyncio.run(cs.search(repo, "o", "n", query=query, top_k=5, embedder=_Embedder()))


def test_far_cards_abstain_off_domain_without_touching_the_fusion() -> None:
    repo = _Repo(card_distance=0.72)  # best card cosine 0.28 < 0.35
    res = _run(repo)
    assert res.abstain is True and res.abstain_code == "off_domain"
    assert "0.28" in res.abstain_reason and res.retrieval_risk == "abstain"
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["cards"]  # three cards for the gate, no chunk read
    assert repo.calls[0][1]["pool"] == 3
    assert res.retrieval_work["card_top_similarity_milli"] == 280
    # retrieval_work is dict[str, int | str] on the wire: a float here fails
    # CodeSearchOutput validation and turns every card-leg search into a 500
    assert all(isinstance(v, (int, str)) for v in res.retrieval_work.values()), res.retrieval_work


def test_close_cards_do_not_abstain_and_the_gate_is_off_at_zero() -> None:
    repo = _Repo(card_distance=0.40)  # cosine 0.60
    res = _run(repo)
    assert res.abstain_code != "off_domain"
    # the card LEG on (ranking) must serialise too
    res = _run(_Repo(card_distance=0.40), code_search_card_leg=True)
    from pydantic import TypeAdapter

    from server.code.api.models_search import CodeSearchOutput

    TypeAdapter(CodeSearchOutput.model_fields["retrieval_work"].annotation).validate_python(
        res.retrieval_work
    )
    assert all(isinstance(v, (int, str)) for v in res.retrieval_work.values()), res.retrieval_work
    repo = _Repo(card_distance=0.72)
    res = _run(repo, code_search_abstain_card_below=0.0)
    assert res.abstain_code != "off_domain" and repo.calls == []


def test_symbol_shaped_queries_skip_the_gate() -> None:
    repo = _Repo(card_distance=0.72)
    res = _run(repo, query="MemoryTracker::allocImpl")
    assert res.abstain_code != "off_domain"
    assert all(c[0] != "cards" for c in repo.calls)
