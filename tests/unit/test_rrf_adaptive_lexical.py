"""RRF adaptive lexical-weight: down-weight the lexical leg for code/path-
shaped queries so rank-fusion doesn't bury the exact-cosine match under rows
that merely share ubiquitous file-path tokens (the ClickGap finding). The
signal is query SHAPE (CamelCase / extensions / paths / digits), which — unlike
mean token-IDF — also catches path queries built from RARE filename tokens."""

from __future__ import annotations

from server.memory.app.retrieval.fusion import _rrf_code_shape, _rrf_lexical_weight_from_shape


def test_prose_query_has_low_shape() -> None:
    assert _rrf_code_shape("secret masking bypassed in EXPLAIN output") < 0.20


def test_code_path_query_has_high_shape() -> None:
    q = (
        "src Storages MergeTree MergeTreeIndexBloomFilter cpp tests queries "
        "0 stateless bloom filter fixed string constant"
    )
    assert _rrf_code_shape(q) > 0.20


def test_rare_filename_query_still_reads_as_code() -> None:
    # Q28 shape: rare CamelCase filenames — the case mean-IDF missed.
    q = (
        "programs local LocalServer cpp src Databases DatabasesCommon cpp "
        "Speed up server shutdown for databases with many tables"
    )
    assert _rrf_code_shape(q) > 0.20


def test_empty_query_shape_is_zero() -> None:
    assert _rrf_code_shape("") == 0.0


def test_prose_keeps_full_lexical_weight() -> None:
    assert _rrf_lexical_weight_from_shape(0.05, lo=0.20, hi=0.30, floor=0.1) == 1.0


def test_code_query_hits_floor() -> None:
    assert abs(_rrf_lexical_weight_from_shape(0.35, lo=0.20, hi=0.30, floor=0.1) - 0.1) < 1e-9


def test_between_is_interpolated() -> None:
    v = _rrf_lexical_weight_from_shape(0.25, lo=0.20, hi=0.30, floor=0.1)  # midpoint
    assert abs(v - (1.0 - 0.5 * 0.9)) < 1e-6  # 0.55


def test_degenerate_hi_le_lo_does_not_divide_by_zero() -> None:
    v = _rrf_lexical_weight_from_shape(0.25, lo=0.30, hi=0.30, floor=0.1)
    assert 0.1 <= v <= 1.0
