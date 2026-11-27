"""A test or doc word asks for tests or docs only in a question, and the lexical
leg cuts its pool on the path penalties the ranking applies."""

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
    asks_for_docs,
    asks_for_tests,
    pool_penalties,
)
from server.code.app.search.window import _saturation_skip_reason
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.index import path_class, query_trim
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.code.store.index.sql import path_penalty_sql

# A bug report as an agent receives it: the reporter says where the fault showed (a
# failing test, what the documentation promises) and asks for a fix in source.
_REPORT = (
    "QuerySet.bulk_update() returns None instead of the number of rows matched. "
    "Calling update() on a queryset returns the count, and the documentation says "
    "bulk_update is a convenience wrapper around update(), so the return value should "
    "be the sum of the counts of each batch. I wrote a failing test for this in my "
    "project and it reproduces on the main branch with sqlite and postgres backends."
)


def test_the_report_is_longer_than_the_lexical_term_list() -> None:
    assert len(code_tokens(_REPORT)) > query_trim.LEXICAL_TOKEN_CAP


@pytest.mark.parametrize(
    "query",
    [
        "how are flaky tests retried",
        "which stateless test covers the query cache",
        "pytest fixture for a fake embedder",
        # naming a test asks for it: the name carries the word
        "test_bulk_update_returns_count",
        "BulkUpdateTests",
        "tests/queries/0_stateless/02494_query_cache.sql",
        "src/Common/tests/gtest_lru.cpp",
    ],
)
def test_a_question_with_a_test_word_asks_for_tests(query: str) -> None:
    assert asks_for_tests(query)


@pytest.mark.parametrize(
    "query",
    ["bulk_update", "QuerySet.bulk_update", "how are parts merged", "latest contest attestation"],
)
def test_a_question_without_a_test_word_does_not(query: str) -> None:
    assert not asks_for_tests(query)


def test_a_test_word_in_a_pasted_report_is_a_mention() -> None:
    assert "test" in code_tokens(_REPORT)
    assert not asks_for_tests(_REPORT)
    # the same sentence alone is a question
    assert asks_for_tests("I wrote a failing test for bulk_update")


def test_the_cut_is_the_lexical_term_list() -> None:
    words = [f"word{chr(97 + i)}" for i in range(26)] + [f"term{chr(97 + i)}" for i in range(5)]
    at_cap = " ".join([*words, "tests"])
    assert len(code_tokens(at_cap)) == query_trim.LEXICAL_TOKEN_CAP
    assert asks_for_tests(at_cap)
    assert not asks_for_tests(" ".join([*words, "extra", "tests"]))


def test_a_doc_word_follows_the_same_rule() -> None:
    assert asks_for_docs("where is the cache documentation")
    assert asks_for_docs("README.md")
    assert not asks_for_docs("how are parts merged")
    assert "documentation" in code_tokens(_REPORT)
    assert not asks_for_docs(_REPORT)


def _u(path: str, score: float, kind: str = "function") -> CodeUnit:
    return CodeUnit(path=path, score=score, kind=kind)


def test_a_report_that_mentions_a_test_keeps_the_test_path_penalty() -> None:
    test = _u("tests/queries/test_bulk_update.py", 0.031)
    impl = _u("django/db/models/query.py", 0.030)
    out = _apply_fusion_signals([test, impl], _REPORT)
    assert out[0].path == impl.path
    by_path = {u.path: u.score for u in out}
    assert by_path[test.path] < 0.031 * _NOISE_PENALTY * 1.5


def test_a_report_that_mentions_the_documentation_keeps_the_prose_penalty() -> None:
    notes = _u("docs/ref/models/querysets.txt", 0.031)
    impl = _u("django/db/models/query.py", 0.030)
    out = _apply_fusion_signals([notes, impl], _REPORT, prose_penalty=0.25)
    assert out[0].path == impl.path


def test_a_report_that_mentions_a_test_keeps_the_kind_prior_and_saturation() -> None:
    header = _u("django/db/models/query.py", 0.031, "file_header")
    impl = _u("django/db/models/query.py", 0.030)
    out, explain = _apply_kind_prior([header, impl], _REPORT, _KIND_PRIOR_TABLES["default"])
    assert out[0].kind == "function"
    assert explain == "kind_prior=default"
    assert _saturation_skip_reason(route="hybrid", path_prefix="", query=_REPORT) == ""


def test_pool_penalties_are_the_factors_the_ranking_applies() -> None:
    assert pool_penalties("how are parts merged", prose_eligible=True) == (_NOISE_PENALTY, 0.25)
    assert pool_penalties("how are flaky tests retried", prose_eligible=True) == (1.0, 0.25)
    assert pool_penalties("where is the cache documentation", prose_eligible=True) == (
        _NOISE_PENALTY,
        1.0,
    )
    # a constrained search or a symbol lookup: the ranking applies no prose penalty
    assert pool_penalties("how are parts merged", prose_eligible=False) == (_NOISE_PENALTY, 1.0)
    assert pool_penalties(_REPORT, prose_eligible=True) == (_NOISE_PENALTY, 0.25)


@pytest.mark.parametrize(
    "path,noise,prose",
    [
        ("tests/queries/0_stateless/02494_query_cache.sql", True, False),
        ("src/Common/tests/gtest_lru.cpp", True, False),
        ("django/test/runner.py", True, False),
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


def test_search_hands_the_leg_the_penalties_of_the_query(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_fusion_signals", True)
    monkeypatch.setattr(settings, "code_search_prose_penalty", 0.25)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    assert _asked("how are parts merged") == (_NOISE_PENALTY, 0.25)
    assert _asked("how are flaky tests retried") == (1.0, 0.25)
    assert _asked("where is the cache documentation") == (_NOISE_PENALTY, 1.0)
    assert _asked(_REPORT) == (_NOISE_PENALTY, 0.25)


def test_the_leg_cuts_on_nothing_the_ranking_does_not_apply(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "code_fusion_signals", False)
    monkeypatch.setattr(settings, "code_search_pr_leg", "off")
    assert _asked("how are parts merged") == (1.0, 1.0)
