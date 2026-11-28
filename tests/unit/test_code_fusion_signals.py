"""Fusion rank priors: path-noise penalty, stem affinity, file coherence."""

from __future__ import annotations

from server.code.app.search.priors import (
    _COHERENCE_FRAC,
    _NOISE_PENALTY,
    _STEM_BOOST_CAP,
    _apply_fusion_signals,
)
from server.code.domain.units import CodeUnit


def _u(path: str, score: float, symbol: str = "") -> CodeUnit:
    return CodeUnit(path=path, score=score, symbol=symbol)


def test_test_dir_demoted_below_equal_impl() -> None:
    impl = _u("src/Storages/MergeTree/MergeTreeData.cpp", 0.030)
    test = _u("src/Storages/tests/gtest_merge_tree.cpp", 0.030)
    out = _apply_fusion_signals([test, impl], "background merge selection")
    assert out[0].path == impl.path
    assert out[-1].score < impl.score


def test_functional_test_queries_dir_demoted() -> None:
    impl = _u("src/Interpreters/Cache/QueryResultCache.cpp", 0.030)
    sql = _u("tests/queries/0_stateless/02494_query_cache.sql", 0.031)
    out = _apply_fusion_signals([sql, impl], "how is the result cached")
    assert out[0].path == impl.path


def test_noise_penalty_demotes_but_never_drops() -> None:
    test = _u("src/Common/tests/gtest_lru.cpp", 0.030)
    out = _apply_fusion_signals([test], "lru cache eviction")
    assert len(out) == 1
    assert out[0].score > 0


def test_stem_affinity_promotes_matching_path() -> None:
    match = _u("src/Interpreters/Cache/QueryResultCache.cpp", 0.030)
    other = _u("src/Processors/Merges/MergingSortedTransform.cpp", 0.030)
    out = _apply_fusion_signals([other, match], "query result cache eviction")
    assert out[0].path == match.path


def test_stem_boost_is_capped() -> None:
    match = _u("src/Interpreters/Cache/QueryResultCache.cpp", 0.030)
    out = _apply_fusion_signals([match], "query result cache")
    # full overlap: x(1 + cap), plus its own coherence share of max
    ceiling = 0.030 * (1 + _STEM_BOOST_CAP) * (1 + _COHERENCE_FRAC) + 1e-9
    assert out[0].score <= ceiling


def test_coherence_concentrates_multi_chunk_file_on_best() -> None:
    a1 = _u("src/A.cpp", 0.020, symbol="A::f")
    a2 = _u("src/A.cpp", 0.019, symbol="A::g")
    a3 = _u("src/A.cpp", 0.018, symbol="A::h")
    b = _u("src/B.cpp", 0.021, symbol="B::f")
    out = _apply_fusion_signals([b, a1, a2, a3], "zzz qqq")
    # A.cpp's aggregate evidence (0.057) beats B.cpp's single chunk: A's
    # best chunk overtakes; A's OTHER chunks do not.
    assert out[0].symbol == "A::f"
    assert [u.symbol for u in out[1:]][0] == "B::f"


def test_penalised_file_cannot_buy_back_via_chunk_count() -> None:
    impl = _u("src/A.cpp", 0.020, symbol="A::f")
    tests = [_u("src/tests/gtest_a.cpp", 0.020, symbol=f"T::t{i}") for i in range(5)]
    out = _apply_fusion_signals([impl, *tests], "zzz qqq")
    # coherence runs over penalised scores: 5 x (0.020*0.5) = 0.050 sum for
    # the test file vs 0.020 for impl — but the test chunks each sit at
    # 0.010 base, so impl (0.020 + its coherence share) stays on top.
    assert out[0].path == impl.path
    assert _NOISE_PENALTY < 1.0


def test_empty_input_is_noop() -> None:
    assert _apply_fusion_signals([], "anything") == []


def test_ties_keep_fused_order() -> None:
    a = _u("src/X.cpp", 0.030, symbol="X::a")
    b = _u("src/Y.cpp", 0.030, symbol="Y::b")
    out = _apply_fusion_signals([a, b], "zzz qqq")
    # identical scores and no signal deltas beyond symmetric coherence:
    # RRF order survives the stable sort
    assert [u.symbol for u in out] == ["X::a", "Y::b"]


def test_embedded_identifier_extraction_shapes() -> None:
    from server.code.domain.idents import embedded_idents

    got = embedded_idents(
        "how does PipelineExecutor use parse_quota via DB::MergeTreeData::renameTempPart"
    )
    assert {"pipelineexecutor", "parse_quota", "mergetreedata", "renametemppart"} <= got
    # plain English never triggers it
    assert embedded_idents("how does the query cache decide what is cacheable") == frozenset()


def test_embedded_symbol_boosts_owning_unit() -> None:
    owner = _u("src/Processors/Executors/X.cpp", 0.030, symbol="DB::PipelineExecutor::execute")
    other = _u("src/Interpreters/Y.cpp", 0.030, symbol="DB::Context::getSettings")
    out = _apply_fusion_signals(
        [other, owner], "how does PipelineExecutor schedule zzz onto worker threads"
    )
    assert out[0].symbol.endswith("PipelineExecutor::execute")


def test_embedded_symbol_ignores_mere_mentions() -> None:
    # a unit whose SCOPE is unrelated is not boosted even if its body would
    # mention the identifier — the signal reads metadata, not content
    caller = _u("src/A.cpp", 0.030, symbol="DB::Unrelated::run")
    out = _apply_fusion_signals([caller], "what does PipelineExecutor do")
    base = _apply_fusion_signals([caller], "what does zzz qqq do")
    assert out[0].score == base[0].score


def test_test_intent_query_is_not_penalized() -> None:
    """The penalty exists for implementation questions drowned by test
    files — a question ABOUT the tests must not have its best candidates
    halved (on the multilang lane, r@10 goes from 1/5 to 4/5 with the
    penalty skipped for test-intent queries)."""
    harness = _u("tests/clickhouse-test", 0.030)
    impl = _u("src/Interpreters/InterpreterSelectQuery.cpp", 0.030)
    out = _apply_fusion_signals(
        [impl, harness], "how does clickhouse-test schedule tests in parallel"
    )
    by_path = {u.path: u.score for u in out}
    # no demotion under test intent: the harness file's score is not halved
    assert by_path["tests/clickhouse-test"] > 0.030 * _NOISE_PENALTY * 1.5


def test_ci_intent_spares_ci_workflow_paths() -> None:
    hook = _u("ci/jobs/scripts/workflow_hooks/filter_job.py", 0.030)
    out = _apply_fusion_signals([hook], "how does CI skip jobs for changed files")
    assert out[0].score >= 0.030  # never demoted (ci/ isn't noise-dir anyway)


def test_implementation_query_still_demotes_tests() -> None:
    impl = _u("src/Storages/MergeTree/MergeTreeData.cpp", 0.030)
    test = _u("src/Storages/tests/gtest_merge_tree.cpp", 0.030)
    out = _apply_fusion_signals([test, impl], "background merge selection")
    assert out[0].path == impl.path


def test_prose_file_demoted_below_the_source_it_describes() -> None:
    notes = _u("docs/releases/4.2.txt", 0.031)
    impl = _u("django/db/models/query.py", 0.030)
    out = _apply_fusion_signals([notes, impl], "QuerySet.update crashes", prose_penalty=0.25)
    assert out[0].path == impl.path


def test_prose_penalty_skips_documentation_questions() -> None:
    notes = _u("docs/topics/cache.txt", 0.031)
    impl = _u("django/core/cache/backends/base.py", 0.030)
    out = _apply_fusion_signals(
        [notes, impl], "where is the cache documentation", prose_penalty=0.25
    )
    assert out[0].path == notes.path


def test_prose_paths_are_matched_by_file_not_by_directory_word() -> None:
    from server.code.app.search.priors import is_prose_path

    assert is_prose_path("doc/en/changelog.rst")
    assert is_prose_path("CHANGES")
    assert is_prose_path("README.md")
    assert not is_prose_path("src/Coordination/Changelog.cpp")
    assert not is_prose_path("CMakeLists.txt")
    assert not is_prose_path("docs/conf.py")


def test_prose_penalty_reads_code_search_prose_penalty(monkeypatch) -> None:
    from server.code.app.search.priors import _prose_penalty
    from server.settings import settings

    monkeypatch.setattr(settings, "code_search_prose_penalty", 0.4)
    assert _prose_penalty(eligible=True) == 0.4
    assert _prose_penalty(eligible=False) == 1.0
