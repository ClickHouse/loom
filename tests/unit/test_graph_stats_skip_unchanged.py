"""The graph-stats pass re-emits a file card only when its
rank or degrees moved."""

from __future__ import annotations

from server.code.workers.graph_stats import _rank_unchanged


def test_equal_within_float_noise_is_unchanged() -> None:
    live = {"pagerank": 0.00123456789, "in_degree": 4, "out_degree": 7}
    assert _rank_unchanged(live, 0.00123456789, 4, 7)
    assert _rank_unchanged(live, 0.00123456789 * (1 + 1e-9), 4, 7)


def test_a_moved_rank_or_degree_is_rewritten() -> None:
    live = {"pagerank": 0.001, "in_degree": 4, "out_degree": 7}
    assert not _rank_unchanged(live, 0.0011, 4, 7)
    assert not _rank_unchanged(live, 0.001, 5, 7)
    assert not _rank_unchanged(live, 0.001, 4, 6)


def test_a_card_without_a_rank_gets_one() -> None:
    assert not _rank_unchanged({"pagerank": None, "in_degree": 0, "out_degree": 0}, 0.2, 0, 0)
    assert _rank_unchanged({"pagerank": None, "in_degree": None, "out_degree": None}, 0.0, 0, 0)
