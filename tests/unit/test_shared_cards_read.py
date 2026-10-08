"""In best_chunk mode the card leg and the concept leg share one cards read."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.search import cards as cs_cards
from server.code.app.search.cards import _card_and_concept_rows
from server.settings import settings


class _Repo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def vector_search_cards(
        self, org, ns, model, vec, *, path_prefix="", pool=15, skip_tests=False
    ):
        self.calls.append(("cards", pool, skip_tests))
        return [{"path": f"src/f{i}.cpp", "distance": 0.1 + i / 100} for i in range(pool)]

    async def best_chunks_for_paths(self, org, ns, model, vec, paths, *, per_path=1):
        self.calls.append(("best_chunks", tuple(paths)))
        return [{"path": p, "chunk_id": f"c{i}", "start_line": 1} for i, p in enumerate(paths)]

    async def symbol_names_for_paths(self, org, ns, paths, limit=200):
        self.calls.append(("names", tuple(paths)))
        return []


async def _vec() -> list[float]:
    return [0.1, 0.2]


def _run(repo: _Repo, monkeypatch, mode: str, files: int = 8):
    monkeypatch.setattr(settings, "code_search_card_leg_mode", mode)
    monkeypatch.setattr(settings, "code_search_card_leg_files", files)

    async def _concepts(*a, **k):
        return []

    monkeypatch.setattr(cs_cards, "_concept_leg_rows", _concepts)
    work: dict[str, Any] = {}
    card_stats: dict[str, Any] = {}
    cards, _ = asyncio.run(
        _card_and_concept_rows(
            repo,
            "o",
            "n",
            "text-embedding-3-large",
            query="q",
            query_vec=_vec,
            use_cards=True,
            card_gate=0.0,
            concept_model="cards:5",
            path_prefix="",
            scope=None,
            work=work,
            card_stats=card_stats,
            concept_stats={},
        )
    )
    return cards, work, card_stats


def test_best_chunk_mode_reads_cards_once_with_the_longer_limit(monkeypatch) -> None:
    repo = _Repo()
    cards, work, card_stats = _run(repo, monkeypatch, "best_chunk")
    reads = [c for c in repo.calls if c[0] == "cards"]
    assert reads == [("cards", 8, True)]
    assert work["card_queries"] == 1 and work["card_read_shared"] == 1
    # the card leg saw all eight files, the concept leg the nearest five
    assert card_stats["files"] == 8 and work["concept_term_files"] == 5
    names = [c for c in repo.calls if c[0] == "names"][0]
    assert names[1] == tuple(f"src/f{i}.cpp" for i in range(5))
    assert len(cards) == 8


def test_skeleton_mode_keeps_two_reads(monkeypatch) -> None:
    repo = _Repo()
    repo.chunks_for_paths = lambda *a, **k: _empty()  # type: ignore[attr-defined]
    _run(repo, monkeypatch, "skeleton")
    reads = sorted(c for c in repo.calls if c[0] == "cards")
    assert reads == [("cards", 5, True), ("cards", 8, False)]


async def _empty():
    return []


def test_a_failed_concept_term_read_drops_the_concept_vote_not_the_search(monkeypatch) -> None:
    """The concept leg is a vote: its symbol-name read failing leaves it
    without terms (and says so), while the card leg's rows still arrive."""

    class _Failing(_Repo):
        async def symbol_names_for_paths(self, org, ns, paths, limit=200):
            raise RuntimeError("Code: 241. DB::Exception: memory limit exceeded")

    seen_terms: list[list[str]] = []

    async def _concepts(*a, **k):
        seen_terms.append(list(k["terms"]))
        return []

    monkeypatch.setattr(settings, "code_search_card_leg_mode", "best_chunk")
    monkeypatch.setattr(settings, "code_search_card_leg_files", 8)
    monkeypatch.setattr(cs_cards, "_concept_leg_rows", _concepts)
    work: dict[str, Any] = {}
    cards, concepts = asyncio.run(
        _card_and_concept_rows(
            _Failing(),
            "o",
            "n",
            "text-embedding-3-large",
            query="q",
            query_vec=_vec,
            use_cards=True,
            card_gate=0.0,
            concept_model="cards:5",
            path_prefix="",
            scope=None,
            work=work,
            card_stats={},
            concept_stats={},
        )
    )
    assert len(cards) == 8 and concepts == []
    assert seen_terms == [[]] and work["concept_leg_failed"] == 1


def test_the_query_embedding_no_leg_awaits_is_released() -> None:
    """A deadline that drops every leg awaiting the shared embedding leaves the
    provider call running; releasing the memo after the legs cancels it."""
    from server.code.app.search.vector import _QueryVecMemo

    class _Slow:
        async def embed(self, text: str) -> list[float]:
            await asyncio.sleep(10)
            return [0.0]

    async def _scenario() -> asyncio.Future[list[float]]:
        memo = _QueryVecMemo("q", {"embedding_calls": 0})
        emb = _Slow()
        leg = asyncio.ensure_future(memo(emb))
        await asyncio.sleep(0)
        leg.cancel()  # the deadline drops the only awaiting leg
        await asyncio.gather(leg, return_exceptions=True)
        fut = memo._futures[id(emb)]
        assert not fut.done()  # shielded: still running with no owner
        memo.release()
        await asyncio.gather(fut, return_exceptions=True)
        return fut

    assert asyncio.run(_scenario()).cancelled()
