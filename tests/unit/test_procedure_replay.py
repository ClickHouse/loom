"""The trace-replay bench at a small size: consolidation converges on the
working branch, contrast flags the failing one, the gate reverts a harmful
revision and accepts a helpful one. Pinned across a few seeds so the result
is a property, not a lucky draw."""

from __future__ import annotations

import pytest

from bench.procedure_replay import replay, utility_selection


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_replay_converges_flags_and_gates(seed: int) -> None:
    r = replay(runs=40, noise=0.1, seed=seed)
    assert r.dominant == ["start", "good", "finish"]
    assert "bad" in r.fail_prone_nodes and "good" not in r.fail_prone_nodes
    assert ("start", "bad") in r.fail_prone_edges
    assert r.bad_gate == "revert"
    assert r.good_gate == "accept"


@pytest.mark.parametrize("seed", [1, 7, 42])
def test_utility_lets_the_proven_procedure_overtake_the_more_similar_one(seed: int) -> None:
    """At weight 0 similarity decides; at weight 1 the ledger
    does, within a handful of runs, and the sign of each utility is right."""
    r = utility_selection(runs=40, seed=seed)
    assert not r.leads_at_weight_zero
    assert 0 < r.first_run_good_leads <= 12
    assert r.utility_good > 0 > r.utility_bad
