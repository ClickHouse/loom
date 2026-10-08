"""Chunk-kind rank prior: multiplicative, bounded, gated on
query shape; default off; the request override picks an arm."""

from __future__ import annotations

import pytest

from server.code.app.search.priors import (
    _KIND_PRIOR_TABLES,
    _STRUCTURAL_KIND_BOOST,
    _apply_kind_prior,
    _kind_prior_table,
    query_names_file,
    structural_kind,
)
from server.code.domain.units import CodeUnit


def _u(path: str, kind: str, score: float, symbol: str = "") -> CodeUnit:
    return CodeUnit(path=path, kind=kind, score=score, symbol=symbol)


def test_tables_are_bounded_and_ordered() -> None:
    for name in ("default", "flat"):
        t = _KIND_PRIOR_TABLES[name]
        assert set(t) == {"function", "class_skeleton", "other", "comment_block", "file_header"}
        assert all(0.6 <= v <= 1.0 for v in t.values()), t
        assert t["function"] >= t["class_skeleton"] >= t["other"] >= t["comment_block"]
        assert t["file_header"] == min(t.values())
    # flat is flatter: every distance from 1.0 is smaller or equal
    for k, v in _KIND_PRIOR_TABLES["flat"].items():
        assert 1.0 - v <= 1.0 - _KIND_PRIOR_TABLES["default"][k]
    assert _KIND_PRIOR_TABLES["structural"] == {}
    assert 1.0 + _STRUCTURAL_KIND_BOOST <= 1.15 + 1e-9


def test_unset_is_the_flat_table_and_unknown_names_are_off() -> None:
    # The flat table cleared the paired gate on the issue lane
    assert _kind_prior_table(None) is _KIND_PRIOR_TABLES["flat"]
    assert _kind_prior_table("off") is None  # request override wins
    assert _kind_prior_table("deafult") is None  # a typo is not an arm
    assert _kind_prior_table("default") is _KIND_PRIOR_TABLES["default"]
    assert _kind_prior_table("STRUCTURAL") == {}


def test_header_demoted_below_the_function_that_implements() -> None:
    header = _u("src/Storages/MergeTree/MergeTreeData.h", "file_header", 0.032)
    impl = _u("src/Storages/MergeTree/MergeTreeData.cpp", "function", 0.030, "DB::MergeTreeData::x")
    out, explain = _apply_kind_prior(
        [header, impl], "how are parts loaded", _KIND_PRIOR_TABLES["default"]
    )
    assert [u.kind for u in out] == ["function", "file_header"]
    assert out[-1].score == pytest.approx(0.032 * 0.6)
    assert explain == "kind_prior=default"


def test_prior_is_multiplicative_and_never_drops() -> None:
    units = [
        _u(f"src/{k}.cpp", k, 0.030) for k in ("function", "class_skeleton", "other", "file_header")
    ]
    out, _ = _apply_kind_prior(units, "zzz qqq", _KIND_PRIOR_TABLES["default"])
    by_kind = {u.kind: u.score for u in out}
    assert len(out) == 4 and all(s > 0 for s in by_kind.values())
    for k, f in _KIND_PRIOR_TABLES["default"].items():
        if k in by_kind:
            assert by_kind[k] == pytest.approx(0.030 * f)


def test_unknown_kind_is_untouched() -> None:
    sym = _u("src/A.cpp", "symbol", 0.030)
    out, _ = _apply_kind_prior([sym], "zzz qqq", _KIND_PRIOR_TABLES["default"])
    assert out[0].score == 0.030


def test_query_naming_a_file_skips_the_prior() -> None:
    header = _u("src/IO/WriteBufferFromS3.h", "file_header", 0.032)
    impl = _u("src/IO/WriteBufferFromS3.cpp", "function", 0.030)
    out, explain = _apply_kind_prior(
        [header, impl], "why does src/IO/WriteBufferFromS3.cpp retry", _KIND_PRIOR_TABLES["default"]
    )
    assert [u.kind for u in out] == ["file_header", "function"]
    assert out[0].score == 0.032
    assert explain == "kind_prior=default:skip_file"


@pytest.mark.parametrize(
    "q,named",
    [
        ("why does src/IO/WriteBufferFromS3.cpp retry", True),
        ("parseQuery.cpp:120 loop", True),
        ('fix for "MergeTreeData.h"', True),
        ("what is in docs/en/sql.md", True),
        ("how does the query cache work", False),
        ("WriteBufferFromS3 upload", False),
        ("the s3 reader", False),
    ],
)
def test_query_names_file(q: str, named: bool) -> None:
    assert query_names_file(q) is named


def test_test_intent_skips_the_prior() -> None:
    sql = _u("tests/queries/0_stateless/02494_query_cache.sql", "other", 0.031)
    impl = _u("src/Interpreters/Cache/QueryResultCache.cpp", "function", 0.030)
    out, explain = _apply_kind_prior(
        [sql, impl], "which stateless test covers the query cache", _KIND_PRIOR_TABLES["default"]
    )
    assert out[0].kind == "other" and out[0].score == 0.031
    assert explain == "kind_prior=default:skip_test_intent"


@pytest.mark.parametrize(
    "q,kind",
    [
        ("the class that caches marks", "class_skeleton"),
        ("the struct holding merge tree settings", "class_skeleton"),
        ("function that parses a query", "function"),
        ("the method that accounts an allocation", "function"),
        ("the method of the class that owns parts", "class_skeleton"),
        ("how does the query cache work", ""),
    ],
)
def test_structural_kind(q: str, kind: str) -> None:
    assert structural_kind(q) == kind


def test_structural_phrasing_prefers_named_kind_instead_of_table() -> None:
    header = _u("src/Storages/MarkCache.h", "file_header", 0.030)
    skel = _u("src/Storages/MarkCache.h", "class_skeleton", 0.029, "DB::MarkCache")
    fn = _u("src/Storages/MergeTree/MergeTreeMarksLoader.cpp", "function", 0.030)
    out, explain = _apply_kind_prior(
        [header, fn, skel], "the class that caches marks", _KIND_PRIOR_TABLES["default"]
    )
    assert out[0].kind == "class_skeleton"
    assert out[0].score == pytest.approx(0.029 * (1 + _STRUCTURAL_KIND_BOOST))
    # the table did NOT apply: header and function keep their fused scores
    assert {u.kind: u.score for u in out[1:]} == {"file_header": 0.030, "function": 0.030}
    assert explain == "kind_prior=default:structural=class_skeleton"


def test_structural_arm_is_identity_without_phrasing() -> None:
    header = _u("src/A.h", "file_header", 0.032)
    impl = _u("src/A.cpp", "function", 0.030)
    out, explain = _apply_kind_prior(
        [header, impl], "how are parts loaded", _KIND_PRIOR_TABLES["structural"]
    )
    assert [u.score for u in out] == [0.032, 0.030]
    assert explain == "kind_prior=structural"
    out, explain = _apply_kind_prior(
        [header, impl], "the function that loads parts", _KIND_PRIOR_TABLES["structural"]
    )
    assert out[0].kind == "function"
    assert explain == "kind_prior=structural:structural=function"


def test_ties_keep_fused_order() -> None:
    a = _u("src/X.cpp", "function", 0.030, "X::a")
    b = _u("src/Y.cpp", "function", 0.030, "Y::b")
    out, _ = _apply_kind_prior([a, b], "zzz qqq", _KIND_PRIOR_TABLES["default"])
    assert [u.symbol for u in out] == ["X::a", "Y::b"]


def test_empty_input_is_noop() -> None:
    assert _apply_kind_prior([], "anything", _KIND_PRIOR_TABLES["default"]) == (
        [],
        "kind_prior=default",
    )


def test_request_model_accepts_arm_names() -> None:
    from server.code.api.models_search import CodeSearchInput

    assert CodeSearchInput(org="o", namespace="n", query="q").kind_prior is None
    assert (
        CodeSearchInput(org="o", namespace="n", query="q", kind_prior="flat").kind_prior == "flat"
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CodeSearchInput(org="o", namespace="n", query="q", kind_prior="loud")


def test_memo_key_covers_the_arm() -> None:
    from server.code.app.search_memo import _SHAPING_FIELDS

    assert "kind_prior" in _SHAPING_FIELDS
