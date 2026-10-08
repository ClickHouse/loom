"""bench/bootstrap.py — the paired, clustered bootstrap a ranking change
ships on — and the code-intel bench's paired comparison arm built on it."""

from __future__ import annotations

from bench.bootstrap import cluster_of_path, paired_bootstrap
from bench.code_intel_qa import Question, Retrieval, compare_arms


def test_all_positive_deltas_have_a_positive_lower_bound() -> None:
    b = paired_bootstrap([0.2] * 20 + [0.4] * 10, resamples=2000)
    assert b.n == 30 and b.clusters == 30
    assert abs(b.mean - (0.2 * 20 + 0.4 * 10) / 30) < 1e-9
    assert b.lb > 0 and b.significant_gain and b.ub >= b.lb
    assert (b.wins, b.ties, b.losses) == (30, 0, 0)


def test_one_question_out_of_sixty_is_noise() -> None:
    # 59 ties and one win: the point estimate is +1.7pp, the bound spans zero
    b = paired_bootstrap([0.0] * 59 + [1.0], resamples=4000)
    assert abs(b.mean - 1 / 60) < 1e-9
    assert b.lb <= 0.0 < b.ub
    assert not b.significant_gain
    assert "+1.7pp" in b.line(unit="pp", scale=100.0)


def test_bootstrap_is_deterministic_under_seed_and_handles_empty() -> None:
    d = [0.1, -0.2, 0.3, 0.0, 0.05, -0.1]
    assert paired_bootstrap(d, seed=3) == paired_bootstrap(d, seed=3)
    e = paired_bootstrap([])
    assert e.n == 0 and e.lb == e.ub == 0.0


def test_clusters_widen_the_interval() -> None:
    # 40 items, 4 clusters that move together: fewer independent draws, wider bound
    deltas = [0.5] * 10 + [-0.5] * 10 + [0.5] * 10 + [-0.5] * 10
    flat = paired_bootstrap(deltas, resamples=3000)
    clustered = paired_bootstrap(deltas, [f"c{i // 10}" for i in range(40)], resamples=3000)
    assert clustered.clusters == 4 and flat.clusters == 40
    assert (clustered.ub - clustered.lb) > (flat.ub - flat.lb)


def test_cluster_of_path() -> None:
    assert cluster_of_path("src/Storages/MergeTree/MergeTreeData.cpp") == "src/Storages"
    assert cluster_of_path("src/Access") == "src/Access"
    assert cluster_of_path("README.md") == "README.md"
    assert cluster_of_path("") == "(none)"


def _q(qid: str, gold: str, qtype: str = "conceptual") -> Question:
    return Question(qid=qid, qtype=qtype, query="q", gold={"paths": [gold]})


def test_compare_arms_is_paired_and_skips_errors_and_non_conceptual() -> None:
    qs = [
        _q("a", "src/A/x.cpp"),
        _q("b", "src/A/y.cpp"),
        _q("c", "src/B/z.cpp"),
        _q("s", "src/S/s.cpp", qtype="symbol_def"),
        _q("e", "src/E/e.cpp"),
    ]
    arm_a = [
        (qs[0], Retrieval(paths=["src/other.cpp"])),  # miss
        (qs[1], Retrieval(paths=["src/A/y.cpp"])),  # hit
        (qs[2], Retrieval(paths=["src/B/z.cpp"])),  # hit
        (qs[3], Retrieval(paths=["src/S/s.cpp"])),  # symbol_def: ignored
        (qs[4], Retrieval(paths=[], error="timeout")),  # errored: skipped
    ]
    arm_b = [
        (qs[0], Retrieval(paths=["src/A/x.cpp"])),  # fixed: +1
        (qs[1], Retrieval(paths=["src/A/y.cpp"])),  # same
        (qs[2], Retrieval(paths=["src/nope.cpp"])),  # broken: -1
        (qs[3], Retrieval(paths=["src/S/s.cpp"])),
        (qs[4], Retrieval(paths=["src/E/e.cpp"])),
    ]
    cmp = compare_arms(arm_a, arm_b)
    assert cmp["n"] == 3
    assert abs(cmp["recall5_a"] - 2 / 3) < 1e-9 and abs(cmp["recall5_b"] - 2 / 3) < 1e-9
    assert cmp["moved"] == [("c", -1.0), ("a", 1.0)]
    b = cmp["boot"]
    assert (b.wins, b.ties, b.losses) == (1, 1, 1) and b.clusters == 2  # src/A, src/B
    assert b.lb < 0 < b.ub and abs(b.mean) < 1e-9
    # recall@10 and MRR ride along on the same pairs and clusters
    sec = cmp["secondary"]
    assert sec["recall@10"]["boot"].n == sec["mrr"]["boot"].n == 3
    assert abs(sec["recall@10"]["a"] - 2 / 3) < 1e-9 and abs(sec["recall@10"]["b"] - 2 / 3) < 1e-9
    assert abs(sec["mrr"]["a"] - 2 / 3) < 1e-9 and abs(sec["mrr"]["b"] - 2 / 3) < 1e-9
