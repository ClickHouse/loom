"""A question predicts a time window, not an anchor day."""

from __future__ import annotations

import pytest

from server.memory.domain.question_window import (
    TimeWindow,
    logistic_window_score,
    resolve_time_window,
    window_gap_days,
)

OBS = "2023-05-23"  # a Tuesday


@pytest.mark.parametrize(
    "q, start, end, kind",
    [
        ("What did Joe buy on 2023-05-20?", "2023-05-20", "2023-05-20", "day"),
        ("What did Melanie do last week?", "2023-05-16", "2023-05-22", "span"),
        ("What did Melanie do yesterday?", "2023-05-22", "2023-05-22", "day"),
        ("Where was Nate last Friday?", "2023-05-19", "2023-05-19", "day"),
        ("Which city did she visit in April 2023?", "2023-04-01", "2023-04-30", "month"),
        ("Where did Nate go in 2022?", "2022-01-01", "2022-12-31", "year"),
        ("What did he do last month?", "2023-04-01", "2023-04-30", "month"),
        ("What did he do two months ago?", "2023-03-01", "2023-03-31", "month"),
        ("What did he do 3 days ago?", "2023-05-20", "2023-05-20", "day"),
        (
            "What happened between March 3 2023 and March 10 2023?",
            "2023-03-03",
            "2023-03-10",
            "span",
        ),
        ("What did she paint from 2023-01-05 to 2023-01-09?", "2023-01-05", "2023-01-09", "span"),
    ],
)
def test_bounded_windows(q, start, end, kind):
    w = resolve_time_window(q, OBS)
    assert w is not None and (w.start, w.end, w.kind) == (start, end, kind)
    assert w.bounded


def test_one_ended_windows():
    w = resolve_time_window("What did she do before 2023-02-01?", OBS)
    assert w is not None and (w.start, w.end) == ("", "2023-02-01") and not w.bounded
    w = resolve_time_window("What has she done since March 2023?", OBS)
    assert w is not None and (w.start, w.end) == ("2023-03-31", OBS)


def test_tense_only_windows_are_open_and_never_bounded():
    assert resolve_time_window("When did Caroline move to Paris?", OBS).kind == "open_past"
    assert resolve_time_window("Will Bob travel soon?", OBS).kind == "open_future"
    assert resolve_time_window("What does Alice usually eat?", OBS).kind == "habitual"
    for q in ("When did Caroline move to Paris?", "Will Bob travel soon?"):
        assert not resolve_time_window(q, OBS).bounded


def test_no_signal_and_no_anchor():
    assert resolve_time_window("What is the capital of France?", OBS) is None
    assert resolve_time_window("", OBS) is None
    # Relative forms need an observation date; absolute ones do not.
    assert resolve_time_window("What did she do last week?", None).kind == "open_past"
    assert resolve_time_window("What happened in 2021?", None).bounded


def test_gap_and_logistic_shape():
    w = TimeWindow("2023-05-01", "2023-05-10", "span")
    from datetime import date

    assert window_gap_days(date(2023, 5, 5), w) == 0
    assert window_gap_days(date(2023, 4, 28), w) == 3
    assert window_gap_days(date(2023, 5, 20), w) == 10
    assert w.days == 10
    # Inside the grace period the score is near the maximum; far away it is
    # the floor of -0.5; the curve is monotone in the gap.
    assert logistic_window_score(0) > 0.85
    assert logistic_window_score(7) == pytest.approx(0.25)
    assert logistic_window_score(400) == pytest.approx(-0.5, abs=1e-6)
    scores = [logistic_window_score(g) for g in range(0, 60, 5)]
    assert scores == sorted(scores, reverse=True)


def test_bare_ranking_is_pure_cosine():
    """Bare semantic mode zeroes every boost, the question window included
    (it measured no gain)."""
    import inspect

    from server.memory.app.retrieval import fact_rank as ms

    src = inspect.getsource(ms._fact_mode_rank_score)
    tail = src[src.index("else:  # semantic: bare") :]
    assert 'rank_score_expr = "(1.0 - distance)"' in tail
    assert "temporal_anchor_boost_expr" not in tail
