"""The two-axis bench's fixture and tally."""

from __future__ import annotations

from bench.procedure_match_axes import PAIRS


def test_fixture_pairs_share_a_precondition_and_differ_in_goal() -> None:
    assert len(PAIRS) >= 10
    for situation, goals, query in PAIRS:
        assert situation.strip() and query.strip() and query != situation
        assert len(goals) == 2
        names = [g[0] for g in goals]
        assert names[0] != names[1]
        for _, steps in goals:
            assert len(steps) >= 2  # procedure.add refuses fewer
