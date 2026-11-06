"""server/code/app/search/ (lexical.py, vector.py, gather.py) — the lexical and
vector legs of code.search run side by side and fuse to the result the serial order
produced."""

from __future__ import annotations

import asyncio
import time
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


# Each fake read or embed sleeps this long. The wall-clock bounds allow 3.4 sleeps,
# under the four a serial run takes, with room for a loaded runner's scheduling.
_SLEEP = 0.2


class _Repo:
    def __init__(self, *, delay: float = 0.0, vector_fails: bool = False) -> None:
        self.delay = delay
        self.vector_fails = vector_fails
        self.span: list[tuple[str, str, float]] = []

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        self.span.append(("stats", "start", time.perf_counter()))
        await asyncio.sleep(self.delay)
        self.span.append(("stats", "end", time.perf_counter()))
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.span.append(("lexical", "start", time.perf_counter()))
        await asyncio.sleep(self.delay)
        self.span.append(("lexical", "end", time.perf_counter()))
        return [_lex("a", 40.0), _lex("b", 10.0)]

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.span.append(("vector", "start", time.perf_counter()))
        await asyncio.sleep(self.delay)
        self.span.append(("vector", "end", time.perf_counter()))
        if self.vector_fails:
            raise RuntimeError("vector index unavailable")
        return [_vec("c", 0.1), _vec("a", 0.2)]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _Embedder:
    model_name = "fake-embed"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return [0.1, 0.2, 0.3]


def _search(repo: _Repo, embedder: _Embedder | None) -> Any:
    # TTL 0 reads the corpus stats on every search: a cached read would leave the
    # lexical chain one sleep long, ending on the very tick the vector read starts.
    with (
        patch.object(settings, "code_fusion_signals", False),
        patch.object(settings, "code_corpus_stats_ttl_s", 0),
    ):
        return asyncio.run(
            cs.search(
                repo,
                "o",
                "n",
                query="where do we retry uploads",
                top_k=3,
                embedder=embedder,
            )
        )


def test_lexical_and_vector_legs_run_side_by_side() -> None:
    repo, emb = _Repo(delay=_SLEEP), _Embedder(delay=_SLEEP)
    t0 = time.perf_counter()
    res = _search(repo, emb)
    wall = time.perf_counter() - t0
    starts = {n: t for n, ev, t in repo.span if ev == "start"}
    ends = {n: t for n, ev, t in repo.span if ev == "end"}
    # The lexical chain is stats → lexical; the vector leg embeds then reads.
    # The embed overlaps the stats read, the vector read overlaps the lexical
    # read: the whole thing takes ~two sleeps, not four.
    assert starts["vector"] < ends["lexical"] and starts["lexical"] < ends["vector"]
    assert starts["lexical"] >= ends["stats"]
    assert wall < 3.4 * _SLEEP, wall
    assert emb.calls == 1
    # Fused as before: both legs present, the shared chunk ranks first.
    ids = [u.unit_id for u in res.units]
    assert ids[0] == "a" and set(ids) == {"a", "b", "c"}


def test_a_failing_vector_leg_still_degrades_to_lexical_only() -> None:
    repo = _Repo(vector_fails=True)
    res = _search(repo, _Embedder())
    assert [u.unit_id for u in res.units] == ["a", "b"]
    # The vector read was attempted (its span is on the record) and the
    # failure degraded to lexical-only instead of failing the search.
    assert [n for n, ev, t in repo.span if ev == "start"] == ["stats", "vector", "lexical"] or set(
        n for n, ev, t in repo.span
    ) == {"stats", "vector", "lexical"}


def test_no_embedder_means_no_vector_leg() -> None:
    repo = _Repo()
    res = _search(repo, None)
    assert [n for n, ev, t in repo.span if ev == "start"] == ["stats", "lexical"]
    assert [u.unit_id for u in res.units] == ["a", "b"]


class _StatsFailRepo(_Repo):
    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        raise RuntimeError("replica refused the read (code 497)")


def test_a_failing_lexical_leg_degrades_to_the_vector_leg() -> None:
    """A corpus-stats failure (e.g. a replica refusing the user) must not fail
    the whole search: the vector leg still answers
    and retrieval_work says the lexical leg failed."""
    repo, emb = _StatsFailRepo(), _Embedder()
    res = _search(repo, emb)
    ids = [u.unit_id for u in res.units]
    assert set(ids) == {"a", "c"} and emb.calls == 1
    assert res.retrieval_work["lexical_leg_failed"] == 1
    assert res.epistemic == "partial"
    assert not [n for n, ev, _ in repo.span if n == "lexical"]


class _FuzzyRepo(_Repo):
    """Records the fuzzy-symbol vote's span; can be told to fail it."""

    def __init__(self, *, delay: float = 0.0, fuzzy_fails: bool = False) -> None:
        super().__init__(delay=delay)
        self.fuzzy_fails = fuzzy_fails

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        self.span.append(("fuzzy", "start", time.perf_counter()))
        await asyncio.sleep(self.delay)
        self.span.append(("fuzzy", "end", time.perf_counter()))
        if self.fuzzy_fails:
            raise RuntimeError("code_symbols scan refused")
        return [{"symbol_id": "s1"}]


def _search_identifier(repo: _Repo, embedder: _Embedder | None) -> Any:
    with patch.object(settings, "code_fusion_signals", False):
        return asyncio.run(
            cs.search(
                repo,
                "o",
                "n",
                query="where do we retry uploadChunk",
                top_k=3,
                embedder=embedder,
            )
        )


def test_the_fuzzy_symbol_vote_runs_beside_the_legs() -> None:
    """The vote needs the query alone, so its scan
    starts with the legs instead of after the slowest one has returned."""
    repo, emb = _FuzzyRepo(delay=_SLEEP), _Embedder(delay=_SLEEP)
    t0 = time.perf_counter()
    res = _search_identifier(repo, emb)
    wall = time.perf_counter() - t0
    starts = {n: t for n, ev, t in repo.span if ev == "start"}
    ends = {n: t for n, ev, t in repo.span if ev == "end"}
    assert starts["fuzzy"] < ends["vector"] and starts["fuzzy"] < ends["lexical"]
    assert wall < 3.4 * _SLEEP, wall
    assert res.retrieval_work["symbol_queries"] == 1
    assert set(u.unit_id for u in res.units) == {"a", "b", "c"}


def test_a_failing_fuzzy_vote_is_no_vote_not_a_failed_search() -> None:
    repo = _FuzzyRepo(fuzzy_fails=True)
    res = _search_identifier(repo, _Embedder())
    assert set(u.unit_id for u in res.units) == {"a", "b", "c"}
    assert res.retrieval_work["symbol_queries"] == 1


def test_a_failed_vector_leg_marks_the_answer_partial() -> None:
    """A leg that raised is not a leg that found nothing: the answer is partial and
    retrieval_work names the leg."""
    res = _search(_Repo(vector_fails=True), _Embedder())
    assert res.retrieval_work["vector_leg_failed"] == 1
    assert res.epistemic == "partial"
    assert _search(_Repo(), _Embedder()).epistemic == ""
