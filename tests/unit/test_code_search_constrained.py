"""Constrained and scoped code.search.

A `pattern` / path filter narrows both legs' pools BEFORE ranking: the
predicates sit in the plain WHERE next to the identity keys (never
PREWHERE), the pool is pre-counted and refused over the
file cap, the memo key carries the fields, calibration knows the pool was
filtered, and an unset scope leaves every statement byte for byte as it
was.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import patch

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.api.models_search import CodeSearchInput
from server.code.app import search as cs
from server.code.app import search_memo as memo
from server.code.domain.search_scope import (
    SearchScope,
    build_scope,
    extensions_for,
    glob_to_re2,
)
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.memory.domain.retrieval_calibration import calibrate_retrieval
from server.settings import settings

# ---- glob converter ----------------------------------------------------------


@pytest.mark.parametrize(
    ("glob", "yes", "no"),
    [
        ("*.cpp", ["a.cpp", "src/x/y.cpp"], ["a.cpph", "a.cppx/b.h", "src/cpp"]),
        ("src/Parsers/*", ["src/Parsers/A.h"], ["src/Parsers/sub/A.h", "x/src/Parsers/A.h"]),
        ("src/Parsers/**", ["src/Parsers/A.h", "src/Parsers/sub/deep/A.h"], ["src/Parser/A.h"]),
        ("**/tests/*.py", ["tests/a.py", "x/y/tests/a.py"], ["tests/sub/a.py", "x/tests.py"]),
        ("*.{h,hpp}", ["a.h", "d/a.hpp"], ["a.hh", "a.cpp"]),
        ("test_?.py", ["test_a.py", "d/test_1.py"], ["test_ab.py", "test_.py"]),
        ("[!t]*.py", ["a.py", "d/x.py"], ["t.py", "d/tx.py"]),
        ("/CMakeLists.txt", ["CMakeLists.txt"], ["src/CMakeLists.txt"]),
        ("**", ["anything", "a/b/c"], []),
    ],
)
def test_glob_to_re2_matches_rg_semantics(glob: str, yes: list[str], no: list[str]) -> None:
    rx = re.compile(glob_to_re2(glob))
    for p in yes:
        assert rx.search(p), (glob, p)
    for p in no:
        assert not rx.search(p), (glob, p)


def test_glob_converter_escapes_regex_metacharacters_and_refuses_bad_globs() -> None:
    assert glob_to_re2("a+b.c") == r"(?:^|/)a\+b\.c$"
    with pytest.raises(ValidationError):
        glob_to_re2("{a,b")
    with pytest.raises(ValidationError):
        glob_to_re2("   ")


def test_langs_expand_to_extension_sets_and_unknown_is_refused() -> None:
    exts = extensions_for(["cpp", "Python"])
    assert ".cpp" in exts and ".hpp" in exts and ".py" in exts
    assert len(exts) == len(set(exts))
    with pytest.raises(ValidationError, match="unknown lang"):
        extensions_for(["cobol"])


# ---- build_scope --------------------------------------------------------------


def test_unset_fields_build_the_empty_scope_and_add_no_sql() -> None:
    sc = build_scope()
    assert not sc.constrained
    assert sc.where_sql() == ("", {})
    assert sc.explain() == ""


def test_build_scope_validates_pattern_and_list_sizes() -> None:
    with pytest.raises(ValidationError):
        build_scope(pattern="x")
    with pytest.raises(ValidationError):
        build_scope(pattern="(", regex=True)
    with pytest.raises(ValidationError):
        build_scope(path_globs=[f"g{i}" for i in range(9)])
    sc = build_scope(pattern="Foo", path_globs=["*.cpp"], exclude_globs=["tests/**"], langs=["cpp"])
    assert sc.constrained and sc.has_pattern and sc.has_path_filters
    assert sc.explain() == "constrained=pattern|globs=1|excludes=1|langs=11ext"


def test_content_condition_takes_the_three_grep_shapes_behind_the_ngram_guard() -> None:
    # The exact matcher is unchanged; the `lower(content) LIKE` guard in front
    # of it is what the n-gram skip index prunes on. A pattern too short for
    # one n-gram gets no guard and the unguarded statement verbatim.
    assert build_scope(pattern="Foo").content_condition() == (
        "lower(content) LIKE {sc_0:String} AND positionCaseInsensitive(content, {sc_pat:String}) > 0",
        {"sc_pat": "Foo", "sc_0": "%foo%"},
    )
    assert build_scope(pattern="Foo", case_sensitive=True).content_condition() == (
        "lower(content) LIKE {sc_0:String} AND position(content, {sc_pat:String}) > 0",
        {"sc_pat": "Foo", "sc_0": "%foo%"},
    )
    assert build_scope(pattern="Fo+", regex=True).content_condition() == (
        "match(content, {sc_pat:String})",
        {"sc_pat": "(?i)Fo+"},
    )
    assert build_scope(pattern="Fo+ok", regex=True).content_condition() == (
        "lower(content) LIKE {sc_0:String} AND match(content, {sc_pat:String})",
        {"sc_pat": "(?i)Fo+ok", "sc_0": "%ok%"},
    ) or build_scope(pattern="Fo+ok", regex=True).content_condition() == (
        "match(content, {sc_pat:String})",
        {"sc_pat": "(?i)Fo+ok"},
    )
    assert build_scope(pattern="Fo+", regex=True, case_sensitive=True).content_condition()[1] == {
        "sc_pat": "Fo+"
    }


# ---- statement shape through a stub client -----------------------------------


class _StubCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.one: dict[str, Any] | None = {"n": 7}

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.calls.append((sql, params or {}))
        return self.one


def _where(sql: str) -> str:
    return sql.split("WHERE", 1)[1].split("ORDER BY")[0]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_legs_put_the_predicates_in_plain_where_after_identity_keys() -> None:
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    sc = build_scope(
        pattern="parseQuotaKey", path_globs=["src/**"], exclude_globs=["*_test.cpp"], langs=["cpp"]
    )
    _run(repo.lexical_search_chunks("o", "n", "m", ["quota"], [1.0], path_prefix="src/", scope=sc))
    _run(repo.vector_search_chunks("o", "n", "m", [0.1, 0.2], path_prefix="src/", scope=sc))
    for sql, params in ch.calls:
        assert "PREWHERE" not in sql
        w = _where(sql)
        assert w.index("org = {org:String}") < w.index("positionCaseInsensitive(content")
        assert "startsWith(path, {prefix:String})" in w
        assert "multiMatchAny(path, {sc_globs:Array(String)})" in w
        assert "NOT multiMatchAny(path, {sc_excl:Array(String)})" in w
        assert "arrayExists(e -> endsWith(path, e), {sc_exts:Array(String)})" in w
        assert params["sc_pat"] == "parseQuotaKey"
        assert params["sc_globs"] == ["^src/(?:.*/)?[^/]*$"] or params["sc_globs"][0].startswith(
            "^src/"
        )
        assert ".hpp" in params["sc_exts"]
    assert (
        "FINAL" in ch.calls[1][0] and "ORDER BY distance ASC" in ch.calls[1][0]
    )  # vector leg keeps FINAL + its bare distance order


def test_unset_scope_leaves_the_leg_statements_unchanged() -> None:
    ch = _StubCH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    _run(repo.lexical_search_chunks("o", "n", "m", ["quota"], [1.0]))
    _run(repo.lexical_search_chunks("o", "n", "m", ["quota"], [1.0], scope=SearchScope()))
    _run(repo.vector_search_chunks("o", "n", "m", [0.1]))
    _run(repo.vector_search_chunks("o", "n", "m", [0.1], scope=None))
    assert ch.calls[0] == ch.calls[1]
    assert ch.calls[2] == ch.calls[3]
    for sql, params in ch.calls:
        assert "sc_" not in sql and not any(k.startswith("sc_") for k in params)


def test_scope_file_count_is_a_uniq_exact_over_the_predicate() -> None:
    ch = _StubCH()
    ch.one = {"n": 412}
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    n = _run(repo.scope_file_count("o", "n", "m", build_scope(pattern="Foo"), path_prefix="src/"))
    assert n == 412
    sql, params = ch.calls[0]
    assert "uniqExact(path)" in sql and "FINAL" in sql
    assert "positionCaseInsensitive(content, {sc_pat:String})" in sql
    assert "embedding_model = {model:String}" in sql and "startsWith(path" in sql
    assert params["sc_pat"] == "Foo" and params["prefix"] == "src/"


def test_a_case_insensitive_regex_keeps_its_index_guard() -> None:
    sql, params = build_scope(pattern=r"getTotals\(\)|setTotals\(", regex=True).content_condition()
    assert sql.startswith("(lower(content) LIKE {sc_0:String} OR lower(content) LIKE")
    likes = {v for k, v in params.items() if k != "sc_pat"}
    assert {"%gettotals()%", "%settotals(%", "%gettotal\u017f()%"} <= likes


def test_grep_takes_path_filters_but_keeps_its_own_content_predicate() -> None:
    ch = _StubCH()
    ch.one = {"n": 3}
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    sc = build_scope(langs=["python"], exclude_globs=["tests/**"])
    _run(repo.grep_chunks("o", "n", "needle", scope=sc))
    _run(repo.grep_files("o", "n", "needle", scope=sc))
    for sql, params in ch.calls:
        assert "position(content, {pat:String}) > 0" in sql
        assert "sc_pat" not in params
        assert "arrayExists(e -> endsWith(path, e), {sc_exts:Array(String)})" in sql
        assert "NOT multiMatchAny(path, {sc_excl:Array(String)})" in sql
    ch2 = _StubCH()
    repo2 = CodeRepo(ch2)  # type: ignore[arg-type]
    _run(repo2.grep_chunks("o", "n", "needle"))
    assert "sc_" not in ch2.calls[0][0]


# ---- service: pre-count, cap, skipped symbol route, calibration flag ----------


class _Repo:
    def __init__(self, *, matched: int = 12) -> None:
        self.matched = matched
        self.calls: list[str] = []
        self.scopes: list[Any] = []

    async def scope_file_count(
        self, org: str, ns: str, model: str, scope: Any, *, path_prefix: str = ""
    ) -> int:
        self.calls.append("count")
        return self.matched

    async def code_corpus_stats(self, org: str, ns: str, tokens: list[str]) -> Any:
        return {t: 3 for t in tokens}, 100, 56.0

    async def lexical_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.calls.append("lexical")
        self.scopes.append(k.get("scope"))
        return [_row("a", lex_score=40.0), _row("b", lex_score=10.0)]

    async def vector_search_chunks(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        self.calls.append("vector")
        self.scopes.append(k.get("scope"))
        return [_row("a", distance=0.2), _row("c", distance=0.3)]

    async def symbols_fuzzy(self, *a: Any, **k: Any) -> list[Any]:
        return []

    async def unit_signals(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    async def symbols_by_name(self, *a: Any, **k: Any) -> list[Any]:
        self.calls.append("symbol")
        return []


def _row(cid: str, **extra: Any) -> dict[str, Any]:
    return {
        "chunk_id": cid,
        "path": f"src/{cid}.cpp",
        "scope": "f",
        "kind": "function",
        "start_line": 1,
        "end_line": 5,
        "content": "x",
        "symbol_ids": [],
        "context_description": "",
        **extra,
    }


class _Embedder:
    model_name = "fake"

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2]


def _search(repo: _Repo, query: str, **kw: Any) -> cs.CodeSearchResult:
    with patch.object(settings, "code_fusion_signals", False):
        return asyncio.run(cs.search(repo, "o", "n", query=query, embedder=_Embedder(), **kw))


def test_pattern_skips_the_symbol_route_and_constrains_both_legs() -> None:
    repo = _Repo(matched=12)
    sc = build_scope(pattern="parseQuotaKey")
    res = _search(repo, "parseQuotaKey", scope=sc)  # symbol-shaped query
    assert repo.calls[0] == "count" and "symbol" not in repo.calls
    assert set(repo.calls) >= {"lexical", "vector"}
    assert all(s is sc for s in repo.scopes)
    assert res.constrained and res.matched_files == 12
    assert res.route.endswith("+pattern")
    assert "constrained=pattern" in res.score_explanation
    assert "matched_files=12" in res.score_explanation
    assert res.retrieval_work["scope_count_queries"] == 1
    assert "constrained=true" in res.calibration_explanation


def test_too_broad_pattern_is_refused_before_any_scan() -> None:
    repo = _Repo(matched=5_000)
    res = _search(
        repo, "how are quotas parsed", scope=build_scope(pattern="the"), pattern_max_files=400
    )
    assert repo.calls == ["count"]
    assert res.abstain and res.abstain_code == "pattern_too_broad"
    assert "5000 files" in res.abstain_reason and "cap 400" in res.abstain_reason
    assert res.matched_files == 5_000 and res.retrieval_work["matched_files"] == 5_000
    assert res.route == "hybrid+pattern"


def test_zero_matched_files_abstains_without_running_the_legs() -> None:
    repo = _Repo(matched=0)
    res = _search(repo, "how are quotas parsed", scope=build_scope(pattern="NoSuchIdent"))
    assert repo.calls == ["count"]
    assert res.abstain and res.abstain_code == "no_match"
    assert "pattern" in res.abstain_reason


def test_unset_scope_keeps_the_default_behaviour() -> None:
    repo = _Repo()
    res = _search(repo, "how are quotas parsed")
    assert "count" not in repo.calls
    assert all(s is None for s in repo.scopes)
    assert not res.constrained and res.matched_files == 0
    assert "+pattern" not in res.route and "constrained" not in res.score_explanation
    assert "constrained" not in res.calibration_explanation


def test_path_filters_alone_skip_the_count_and_suffix_scoped() -> None:
    repo = _Repo()
    res = _search(repo, "how are quotas parsed", scope=build_scope(langs=["cpp"]))
    assert "count" not in repo.calls
    assert res.constrained and res.route.endswith("+scoped")
    assert "constrained=langs" in res.score_explanation


# ---- calibration flag -----------------------------------------------------------


def test_constrained_calibration_does_not_read_a_small_pool_as_thin() -> None:
    plain = calibrate_retrieval(top_similarity=0.6, result_count=2, abstain_threshold=0.3)
    constrained = calibrate_retrieval(
        top_similarity=0.6, result_count=2, abstain_threshold=0.3, constrained=True
    )
    assert constrained.confidence > plain.confidence
    assert "constrained=true" in constrained.explanation
    assert "constrained" not in plain.explanation
    empty = calibrate_retrieval(top_similarity=0.0, result_count=0, constrained=True)
    assert empty.risk == "abstain" and "constraint" in empty.explanation


# ---- memo key ------------------------------------------------------------------


def test_memo_key_includes_every_constrained_field() -> None:
    def key(**kw: Any) -> str:
        inp = CodeSearchInput(query="how are quotas parsed", **kw)
        return memo.key_for(inp, embedding_model="m", rerank="", token_budget=3000, recent_dirs=[])

    base = key()
    assert base != key(pattern="quota")
    assert key(pattern="quota") != key(pattern="quota", regex=True)
    assert key(pattern="quota") != key(pattern="quota", case_sensitive=True)
    assert base != key(path_globs=["*.cpp"])
    assert base != key(exclude_globs=["tests/**"])
    assert base != key(langs=["cpp"])
    for f in ("pattern", "regex", "case_sensitive", "path_globs", "exclude_globs", "langs"):
        assert f in memo._SHAPING_FIELDS


# ---- wire models -----------------------------------------------------------------


def test_inputs_default_unconstrained_and_bound_the_lists() -> None:
    inp = CodeSearchInput(query="q")
    assert inp.pattern == "" and inp.regex is False and inp.case_sensitive is False
    assert inp.path_globs == [] and inp.exclude_globs == [] and inp.langs == []
    from pydantic import ValidationError as PydanticValidationError

    with pytest.raises(PydanticValidationError):
        CodeSearchInput(query="q", path_globs=[f"g{i}" for i in range(9)])
    g = CodeGrepInput(pattern="needle", langs=["cpp"])
    assert g.langs == ["cpp"] and g.path_globs == []


def test_handler_scope_helper_takes_only_path_filters_from_grep() -> None:
    from server.code.app.search.shared import _scope_of

    sc = _scope_of(CodeGrepInput(pattern="needle", regex=True, langs=["cpp"]))
    assert not sc.has_pattern and sc.has_path_filters
    sc2 = _scope_of(CodeSearchInput(query="q", pattern="needle", regex=True, case_sensitive=True))
    assert sc2.has_pattern and sc2.regex and sc2.case_sensitive
    assert not _scope_of(CodeSearchInput(query="q")).constrained


def test_a_regex_pattern_is_validated_as_re2() -> None:
    from server.code.domain.search_scope import build_scope

    with pytest.raises(ValidationError, match="RE2"):
        build_scope(pattern=r"(ab)\1", regex=True)
