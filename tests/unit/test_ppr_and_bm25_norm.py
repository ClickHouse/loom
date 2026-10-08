"""Unit tests for the read-path helpers in
server/memory/app/retrieval/ — all CH-free / pure.

Covers:
  • `_bm25_sigmoid_params` — query-length-adaptive sigmoid params.
"""

from __future__ import annotations

from server.memory.app.search_bm25 import _bm25_sigmoid_params


def test_bm25_sigmoid_params_shape() -> None:
    mid, steep = _bm25_sigmoid_params(3, max_score=10.0)
    assert 0.0 < mid < 10.0
    assert steep > 0.0


def test_bm25_sigmoid_short_queries_are_sharper() -> None:
    _, steep_short = _bm25_sigmoid_params(1, max_score=10.0)
    _, steep_mid = _bm25_sigmoid_params(4, max_score=10.0)
    _, steep_long = _bm25_sigmoid_params(8, max_score=10.0)
    # Fewer terms → sharper (larger) slope.
    assert steep_short > steep_mid > steep_long


def test_bm25_sigmoid_handles_zero_max_score() -> None:
    # Must not divide by zero on a cold corpus.
    mid, steep = _bm25_sigmoid_params(2, max_score=0.0)
    assert steep > 0.0 and mid >= 0.0
