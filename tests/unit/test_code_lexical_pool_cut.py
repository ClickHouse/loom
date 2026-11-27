"""The lexical leg cuts its pool on the path penalties the ranking applies to the
query."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.search import search
from server.code.app.search.priors import (
    _KIND_PRIOR_TABLES,
    _NOISE_PENALTY,
    _apply_fusion_signals,
    _apply_kind_prior,
    _prose_penalty,
    pool_penalties,
)
from server.code.app.search.window import _saturation_skip_reason
from server.code.domain.index import path_class
from server.code.domain.search_scope import SearchScope
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.code.store.index.sql import path_penalty_sql
from server.settings import settings

_IMPLEMENTATION = "how are parts merged"
_TESTS = "how are flaky tests retried"
_DOCS = "where is the cache documentation"
# A prompt of several sentences that asks for tests: its length does not change what
# it asks for.
_LONG_TESTS = (
    "Add a regression test for the cache eviction order when two entries share one "
    "weight. Look at how the existing tests under tests/unit build the fixture and "
    "which helper fills the cache, then put the new case next to the similar ones "
    "for expiry and resize."
)


@pytest.fixture(autouse=True)
def _prose_penalty_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_search_prose_penalty", 0.25)


@pytest.mark.parametrize(
    "query,penalties",
    [
        (_IMPLEMENTATION, (_NOISE_PENALTY, 0.25)),
        (_TESTS, (1.0, 0.25)),
        (_DOCS, (_NOISE_PENALTY, 1.0)),
        (_LONG_TESTS, (1.0, 0.25)),
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


def _u(path: str, score: float, kind: str = "function") -> CodeUnit:
    return CodeUnit(path=path, score=score, kind=kind)


@pytest.mark.parametrize("query", [_IMPLEMENTATION, _TESTS, _DOCS, _LONG_TESTS])
def test_the_ranking_multiplies_by_the_factors_the_pool_is_cut_on(query: str) -> None:
    """One chunk per file and no query word in a path: every unit gets the same
    coherence share, so the score ratios are the path factors alone."""
    noise, prose = pool_penalties(query, prose_eligible=True)
    out = _apply_fusion_signals(
        [_u("pkg/aaa.py", 0.02), _u("examples/bbb.py", 0.02), _u("guide/ccc.md", 0.02)],
        query,
        prose_penalty=_prose_penalty(eligible=True),
    )
    score = {u.path: u.score for u in out}
    assert score["examples/bbb.py"] / score["pkg/aaa.py"] == pytest.approx(noise, rel=1e-3)
    assert score["guide/ccc.md"] / score["pkg/aaa.py"] == pytest.approx(prose, rel=1e-3)


def test_a_long_prompt_that_asks_for_tests_keeps_its_test_intent() -> None:
    header = _u("tests/unit/test_cache.py", 0.031, "file_header")
    case = _u("tests/unit/test_cache.py", 0.030)
    out, explain = _apply_kind_prior([header, case], _LONG_TESTS, _KIND_PRIOR_TABLES["default"])
    assert [u.kind for u in out] == ["file_header", "function"]
    assert explain == "kind_prior=default:skip_test_intent"
    assert _saturation_skip_reason(route="hybrid", path_prefix="", query=_LONG_TESTS) == (
        "test_intent"
    )


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


class _CH:
    """Answers the leg's statement with rows in the order a penalised cut returns
    them: source first, then the test row that outscores it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return [
            {"chunk_id": "src", "path": "src/a.py", "lex_score": 4.0},
            {"chunk_id": "test", "path": "tests/test_a.py", "lex_score": 9.0},
            {"chunk_id": "src2", "path": "src/b.py", "lex_score": 4.0},
        ]


def test_the_pool_is_cut_on_the_penalised_score_and_returned_in_score_order() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    rows = asyncio.run(
        repo.lexical_search_chunks(
            "o", "n", "m", ["merge"], [2.5], noise_penalty=0.35, prose_penalty=0.25
        )
    )
    sql, params = ch.calls[0]
    factor, factor_params = path_penalty_sql("path", 0.35, 0.25)
    assert f"ORDER BY lex_score * {factor} DESC\n            LIMIT {{pool:UInt32}}" in sql
    assert factor_params.items() <= params.items()
    # ranks are by the unpenalised score; equal scores keep the statement's order
    assert [r["chunk_id"] for r in rows] == ["test", "src", "src2"]


def test_without_a_penalty_the_statement_and_the_row_order_are_the_leg_s_own() -> None:
    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    rows = asyncio.run(repo.lexical_search_chunks("o", "n", "m", ["merge"], [2.5]))
    sql, params = ch.calls[0]
    assert "ORDER BY lex_score DESC" in sql
    assert "noise_path" not in params and "match(path" not in sql
    assert [r["chunk_id"] for r in rows] == ["src", "test", "src2"]


_UNIT = {
    "scope": "f",
    "kind": "function",
    "start_line": 1,
    "end_line": 9,
    "content": "x",
    "symbol_ids": [],
    "context_description": "",
}


class _Repo:
    """Records what the lexical leg is asked to cut its pool on."""

    def __init__(self) -> None:
        self.penalties: list[tuple[float, float]] = []

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        return {t: 1 for t in tokens}, 100, 10.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.penalties.append((k["noise_penalty"], k["prose_penalty"]))
        return [{"chunk_id": "a", "lex_score": 4.0, "path": "src/a.py", **_UNIT}]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}


def _asked(query: str, **kw: Any) -> tuple[float, float]:
    repo = _Repo()
    asyncio.run(search(repo, "o", "n", query=query, legs="lexical", **kw))  # type: ignore[arg-type]
    assert len(repo.penalties) == 1
    return repo.penalties[0]


def test_search_hands_the_leg_the_penalties_of_the_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_fusion_signals", True)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    assert _asked(_IMPLEMENTATION) == (_NOISE_PENALTY, 0.25)
    assert _asked(_TESTS) == (1.0, 0.25)
    assert _asked(_DOCS) == (_NOISE_PENALTY, 1.0)
    assert _asked(_LONG_TESTS) == (1.0, 0.25)
    # a search scoped to paths: the ranking applies no prose penalty
    scoped = SearchScope(extensions=("py",))
    assert _asked(_IMPLEMENTATION, scope=scoped) == (_NOISE_PENALTY, 1.0)


def test_the_leg_cuts_on_nothing_the_ranking_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_fusion_signals", False)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    assert _asked(_IMPLEMENTATION) == (1.0, 1.0)
