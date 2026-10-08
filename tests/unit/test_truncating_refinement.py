"""A refinement that drops a measured token keeps the peer."""

from server.memory.app.conflicts import truncating_refinement
from server.memory.app.value_updates import measured_tokens


def test_measured_tokens_read_spelled_measures_and_months() -> None:
    t = measured_tokens(
        "User finished 'The Nightingale', which took them three weeks; paid $40 on 2023-05-24 in March"
    )
    assert "three_weeks" in t and "$40" in t and "mar" in t and "2023-05-24" in t or "2023" in t


def test_enjoyed_does_not_refine_finished_in_three_weeks() -> None:
    old = "User recently finished 'The Nightingale' by Kristin Hannah, which took them three weeks to finish."
    new = "User really enjoyed the emotional depth of 'The Nightingale'."
    assert truncating_refinement(new, old)


def test_a_fuller_statement_still_refines() -> None:
    old = "User's personal best 5K is 27:12."
    new = "User's personal best 5K is 27:12, set at the Midsummer 5K Run on 2023-06-10."
    assert not truncating_refinement(new, old)
    assert not truncating_refinement("anything", "")
