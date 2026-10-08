""" "the past weekend" resolves like "last weekend"."""

from __future__ import annotations

import pytest

from server.memory.domain.query_intent import _LAST_PERIOD_RE


@pytest.mark.parametrize(
    ("q", "unit"),
    [
        ("Which bike did I fix the past weekend?", "weekend"),
        ("How many dinner parties have I attended in the past month?", "month"),
        ("How much faster was I than my previous year's time?", "year"),
        ("What did I do this past week?", "week"),
        ("Which bike did I fix last weekend?", "weekend"),
    ],
)
def test_past_and_previous_periods_resolve(q: str, unit: str) -> None:
    m = _LAST_PERIOD_RE.search(q)
    assert m is not None and m[1] == unit


def test_a_bare_period_word_does_not_resolve() -> None:
    assert _LAST_PERIOD_RE.search("Which weekend did I go hiking?") is None
