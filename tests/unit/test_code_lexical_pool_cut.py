"""The lexical leg cuts its pool on the vote a row has in the ranking: the factor of
its path over the reciprocal-rank constant plus its rank in the leg."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from server.code.app.search import search
from server.code.app.search.fusion import _rrf_fuse, leg_provenance
from server.code.app.search.lexical import _lexical_leg
from server.code.app.search.priors import (
    _NOISE_PENALTY,
    _apply_fusion_signals,
    pool_penalties,
)
from server.code.domain.index import path_class, query_trim
from server.code.domain.index.pool_cut import MAX_DEPTH, RRF_K, cut_depth
from server.code.domain.search_scope import SearchScope
from server.code.domain.units import CodeUnit
from server.code.store.index.sql import path_penalty_sql
from server.settings import settings

_IMPLEMENTATION = "how are parts merged"
_TESTS = "how are flaky tests retried"
_DOCS = "where is the cache documentation"


@pytest.fixture(autouse=True)
def _prose_penalty_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_search_prose_penalty", 0.25)
    monkeypatch.setattr(settings, "code_fusion_signals", True)


@pytest.mark.parametrize(
    "query,penalties",
    [
        (_IMPLEMENTATION, (_NOISE_PENALTY, 0.25)),
        (_TESTS, (1.0, 0.25)),
        (_DOCS, (_NOISE_PENALTY, 1.0)),
        ("docs for the test fixtures", (1.0, 1.0)),
    ],
)
def test_pool_penalties_follow_the_words_of_the_query(
    query: str, penalties: tuple[float, float]
) -> None:
    assert pool_penalties(query, prose_eligible=True) == penalties


def test_a_search_the_ranking_applies_no_prose_penalty_to_cuts_on_none() -> None:
    # constrained to paths, or a symbol lookup
    assert pool_penalties(_IMPLEMENTATION, prose_eligible=False) == (_NOISE_PENALTY, 1.0)


def test_no_penalty_with_the_fusion_signals_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_fusion_signals", False)
    assert pool_penalties(_IMPLEMENTATION, prose_eligible=True) == (1.0, 1.0)


def _u(path: str, score: float, kind: str = "function") -> CodeUnit:
    return CodeUnit(path=path, score=score, kind=kind)


@pytest.mark.parametrize("pair", [(0.35, 0.25), (1.0, 0.25), (0.35, 1.0), (1.0, 1.0)])
def test_the_ranking_multiplies_by_the_pair_it_is_handed(pair: tuple[float, float]) -> None:
    """One chunk per file and no query word in a path: every unit gets the same
    coherence share, so the score ratios are the path factors alone. The query
    asks for tests and docs; only the pair decides."""
    out = _apply_fusion_signals(
        [_u("pkg/aaa.py", 0.02), _u("examples/bbb.py", 0.02), _u("guide/ccc.md", 0.02)],
        "docs for the test fixtures",
        path_penalties=pair,
    )
    score = {u.path: u.score for u in out}
    assert score["examples/bbb.py"] / score["pkg/aaa.py"] == pytest.approx(pair[0], rel=1e-3)
    assert score["guide/ccc.md"] / score["pkg/aaa.py"] == pytest.approx(pair[1], rel=1e-3)


@pytest.mark.parametrize(
    "path,noise,prose",
    [
        ("tests/queries/0_stateless/02494_query_cache.sql", True, False),
        ("src/Common/tests/gtest_lru.cpp", True, False),
        ("pkg/test/runner.py", True, False),
        ("examples/demo.py", True, False),
        ("src/Common/test_lru.cpp", True, False),
        ("src/Common/lru_fuzz.cc", True, False),
        ("src/latest/contest.py", False, False),
        ("src/test_lru.py", False, False),
        ("docs/releases/4.2.txt", False, True),
        ("README.md", False, True),
        ("docs/Guide.MD", False, True),
        ("CHANGES", False, True),
        ("pkg/HISTORY.rst", False, True),
        ("tests/README.md", True, True),
        ("CMakeLists.txt", False, False),
        ("src/cmakelists.TXT", False, False),
        ("src/Coordination/Changelog.cpp", False, False),
        ("pkg/history.py", False, False),
        ("docs/conf.py", False, False),
    ],
)
def test_path_classes(path: str, noise: bool, prose: bool) -> None:
    assert path_class.is_noise_path(path) is noise
    assert path_class.is_prose_path(path) is prose


def test_the_sql_factor_binds_the_python_patterns() -> None:
    sql, params = path_penalty_sql("path", 0.35, 0.25)
    assert sql == (
        "(if(match(path, {noise_path:String}), {noise_penalty:Float64}, 1)"
        " * if(match(path, {prose_path:String})"
        " AND NOT match(path, {prose_exempt:String}), {prose_penalty:Float64}, 1))"
    )
    assert params == {
        "noise_path": path_class.NOISE_PATH_PATTERN,
        "noise_penalty": 0.35,
        "prose_path": path_class.PROSE_PATH_PATTERN,
        "prose_exempt": path_class.PROSE_EXEMPT_PATTERN,
        "prose_penalty": 0.25,
    }


# ---- the depth the scan reads ---------------------------------------------------


@pytest.mark.parametrize(
    "noise,prose,depth",
    [
        (1.0, 1.0, 200),  # no penalty: the pool itself
        (0.35, 1.0, 683),
        (1.0, 0.25, 980),
        (0.35, 0.25, 2912),  # a prose file under a test directory gets both
        (1.0, 2.0, 460),  # a factor over 1 lifts rows from past the pool
        (0.35, 0.01, MAX_DEPTH),
        (0.35, 0.0, MAX_DEPTH),
    ],
)
def test_cut_depth(noise: float, prose: float, depth: int) -> None:
    assert cut_depth(200, noise, prose) == depth


def _vote_pool(factors: list[float], pool: int) -> list[int]:
    """The ranks (1-based) of the `pool` rows with the largest vote."""
    by_vote = sorted(range(len(factors)), key=lambda i: -factors[i] / (RRF_K + i + 1))
    return sorted(i + 1 for i in by_vote[:pool])


@pytest.mark.parametrize("seed", range(20))
def test_no_row_past_the_depth_is_in_the_pool(seed: int) -> None:
    """Any mix of source, test and prose rows in any order: the rows with the largest
    votes all rank within `cut_depth`, so the scan loses none of them."""
    rng = random.Random(seed)
    noise, prose = rng.choice([(0.35, 0.25), (1.0, 0.25), (0.35, 1.0), (0.6, 0.9), (0.5, 1.5)])
    pool = rng.choice([5, 50, 200])
    source_share = rng.choice([0.0, 0.02, 0.15, 0.5, 1.0])
    factors = [
        1.0 if rng.random() < source_share else rng.choice([noise, prose, noise * prose])
        for _ in range(8000)
    ]
    assert max(_vote_pool(factors, pool)) <= cut_depth(pool, noise, prose)


def test_a_first_ranked_test_row_stays_and_the_last_test_rows_leave() -> None:
    """What the cut is for, and what it must not do. 1,000 rows, every fifth a source
    row. Ranked alone, the pool is 160 test rows and 40 source rows. Cut on votes, the
    best test rows stay (the first votes 0.35/61, more than a source row at rank 115)
    and the pool reaches source rows the plain cut never saw."""
    factors = [1.0 if rank % 5 == 0 else 0.35 for rank in range(1, 1001)]
    pool = _vote_pool(factors, 200)
    tests = [r for r in pool if factors[r - 1] < 1.0]
    source = [r for r in pool if factors[r - 1] == 1.0]
    assert pool[:4] == [1, 2, 3, 4]  # the leg's best rows, tests included
    assert len(source) > 40 and max(source) > 200
    # test rows leave from the bottom of the leg's order, never from its top
    every_test = [rank for rank in range(1, 1001) if factors[rank - 1] < 1.0]
    assert 0 < len(tests) < 160 and tests == every_test[: len(tests)]


# ---- what the leg hands the ranking and calibration ------------------------------


def test_a_row_votes_with_its_rank_in_the_whole_leg() -> None:
    """A source row the cut admitted from rank 340 votes 1/(60+340), not with its place
    among the rows that survived; a leg that returns no rank votes by position."""
    cut = [
        {"chunk_id": "test", "lex_rank": 1},
        {"chunk_id": "src", "lex_rank": 2},
        {"chunk_id": "deep", "lex_rank": 340},
    ]
    vector = [{"chunk_id": "deep"}, {"chunk_id": "other"}]
    scores = {r["chunk_id"]: s for r, s in _rrf_fuse(cut, vector)}
    assert scores["deep"] == 1.0 / (RRF_K + 340) + 1.0 / (RRF_K + 1)
    assert scores["src"] == 1.0 / (RRF_K + 2)
    assert scores["other"] == 1.0 / (RRF_K + 2)
    assert leg_provenance(cut, vector)["deep"] == ("both", 340, 1)
    plain = [{"chunk_id": "a"}, {"chunk_id": "b"}]
    assert leg_provenance(plain, [])["b"] == ("lexical", 2, 0)


_UNIT = {
    "scope": "f",
    "kind": "function",
    "start_line": 1,
    "end_line": 9,
    "content": "x",
    "symbol_ids": [],
    "context_description": "",
}


def _row(cid: str, path: str, rank: int, in_pool: bool) -> dict[str, Any]:
    return {
        "chunk_id": cid,
        "path": path,
        "lex_score": 100.0 / rank,
        "lex_rank": rank,
        "in_pool": in_pool,
        **_UNIT,
    }


# What the cut statement returns for a pool of 200: the leg's first rows, of which the
# second (a prose file) lost its pool slot, and a source row the cut admitted from rank 340.
_CUT_ROWS = [
    _row("test", "tests/test_a.py", 1, True),
    _row("notes", "docs/notes.md", 2, False),
    _row("src", "src/a.py", 3, True),
    _row("deep", "src/b.py", 340, True),
]


class _Repo:
    """Answers the lexical leg with `rows` and records the penalties it was asked to
    cut its pool on; the vector leg ranks the prose row the cut left out first."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or [{"chunk_id": "a", "lex_score": 4.0, "path": "src/a.py", **_UNIT}]
        self.penalties: list[tuple[float, float]] = []

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        return {t: 1 for t in tokens}, 100, 10.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.penalties.append((k["noise_penalty"], k["prose_penalty"]))
        return self.rows

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [
            {"chunk_id": "notes", "distance": 0.2, "path": "docs/notes.md", **_UNIT},
            {"chunk_id": "deep", "distance": 0.3, "path": "src/b.py", **_UNIT},
        ]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


class _Embedder:
    model_name = "fake-embed"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _leg(rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    work = {"lexical_queries": 0}
    trim = query_trim.plan(_IMPLEMENTATION, None, 48, work)
    pool, head, _, _ = asyncio.run(
        _lexical_leg(
            _Repo(rows),  # type: ignore[arg-type]
            "o",
            "n",
            "m",
            run_lexical=True,
            tokens=trim.tokens,
            trim=trim,
            work=work,
            path_prefix="",
            scope=None,
            path_penalties=(0.35, 0.25),
        )
    )
    return [r["chunk_id"] for r in pool], [r["chunk_id"] for r in head]


def test_the_leg_returns_the_cut_pool_and_its_own_first_rows() -> None:
    assert _leg(_CUT_ROWS) == (["test", "src", "deep"], ["test", "notes", "src"])


def test_a_leg_that_cut_nothing_returns_one_list_for_both() -> None:
    plain = [{"chunk_id": "a", "path": "src/a.py"}, {"chunk_id": "b", "path": "tests/b.py"}]
    assert _leg(plain) == (["a", "b"], ["a", "b"])


def test_calibration_reads_the_leg_s_own_order_and_the_ranking_reads_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prose row is the leg's second row and the vector leg's first, so the legs
    agree, as they would without the cut. It lost its pool slot, so it is ranked on its
    vector vote alone, and the source row from rank 340 is ranked with both votes."""
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    res = asyncio.run(
        search(_Repo(_CUT_ROWS), "o", "n", query=_IMPLEMENTATION, embedder=_Embedder(), top_k=4)  # type: ignore[arg-type]
    )
    assert "legs_agree=True" in res.score_explanation
    assert "(lexical=3, vector=2)" in res.score_explanation
    found = {u.unit_id: (u.via, u.lexical_rank, u.vector_rank) for u in res.units}
    assert found == {
        "test": ("lexical", 1, 0),
        "notes": ("vector", 0, 1),
        "src": ("lexical", 3, 0),
        "deep": ("both", 340, 2),
    }


def _asked(query: str, **kw: Any) -> tuple[float, float]:
    repo = _Repo()
    asyncio.run(search(repo, "o", "n", query=query, legs="lexical", **kw))  # type: ignore[arg-type]
    assert len(repo.penalties) == 1
    return repo.penalties[0]


def test_search_hands_the_leg_the_penalties_of_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    assert _asked(_IMPLEMENTATION) == (_NOISE_PENALTY, 0.25)
    assert _asked(_TESTS) == (1.0, 0.25)
    assert _asked(_DOCS) == (_NOISE_PENALTY, 1.0)
    # a search scoped to paths: the ranking applies no prose penalty
    scoped = SearchScope(extensions=("py",))
    assert _asked(_IMPLEMENTATION, scope=scoped) == (_NOISE_PENALTY, 1.0)


def test_the_leg_cuts_on_nothing_the_ranking_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_fusion_signals", False)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    assert _asked(_IMPLEMENTATION) == (1.0, 1.0)
