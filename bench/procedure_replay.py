"""Trace-replay bench for the procedure plane (evidence).

Simulates an agent executing a procedure whose graph has a branch that works
and a branch that does not, reports noisy runs, and checks that
consolidation converges on the working branch, that the outcome-contrast
flags land on the failing one, and that a bad revision is reverted by the
acceptance gate while a good one is accepted. Pure: no ClickHouse, no LLM.

    python bench/procedure_replay.py [--runs 60] [--noise 0.1] [--seed 7]

The unit test in tests/unit/test_procedure_replay.py runs the same core at a
small size so the bench cannot silently rot.
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.memory.domain.procedure_graph import (
    ProcedureGraph,
    RunStats,
    RunTrace,
    carry_stats,
    dominant_path,
    evolve_graph,
    gate_revision,
    gate_revision_sprt,
    merge_run,
    normalize_graph,
    utility_from_stats,
)


def diamond() -> ProcedureGraph:
    """start -> {good, bad} -> finish. `good` succeeds, `bad` mostly fails."""
    return normalize_graph(
        {
            "nodes": [
                {"id": "start", "text": "read the ticket"},
                {
                    "id": "good",
                    "kind": "action",
                    "tool": "run_tests",
                    "text": "run the tests first",
                },
                {"id": "bad", "kind": "action", "tool": "push", "text": "push without testing"},
                {"id": "finish", "text": "close the ticket"},
            ],
            "edges": [
                {"src": "start", "dst": "good"},
                {"src": "start", "dst": "bad"},
                {"src": "good", "dst": "finish"},
                {"src": "bad", "dst": "finish"},
            ],
        }
    )


@dataclass
class ReplayResult:
    runs: int
    dominant: list[str]
    fail_prone_nodes: list[str]
    fail_prone_edges: list[tuple[str, str]]
    bad_gate: str
    good_gate: str
    # Sequential gate: runs on the new side until a verdict, and the verdict,
    # for the harmful and the helpful revision.
    sprt_bad: tuple[str, int] = ("undecided", 0)
    sprt_good: tuple[str, int] = ("undecided", 0)


def runs_to_verdict(
    before: RunStats,
    *,
    p_ok: float,
    rng: random.Random,
    max_runs: int = 200,
) -> tuple[str, int]:
    """Feed Bernoulli(p_ok) runs one at a time into the sequential gate and
    return (verdict, runs consumed)."""
    after = RunStats()
    for i in range(1, max_runs + 1):
        ok = rng.random() < p_ok
        after = RunStats(total=after.total + 1, ok=after.ok + (1 if ok else 0))
        verdict, _ = gate_revision_sprt(before, after)
        if verdict != "undecided":
            return verdict, i
    return "undecided", max_runs


def simulate(
    graph: ProcedureGraph,
    *,
    runs: int,
    p_bad_branch: float,
    p_good_ok: float,
    p_bad_ok: float,
    rng: random.Random,
) -> tuple[ProcedureGraph, RunStats]:
    stats = RunStats()
    log: list[RunTrace] = []
    for _ in range(runs):
        via = "bad" if rng.random() < p_bad_branch else "good"
        ok = rng.random() < (p_bad_ok if via == "bad" else p_good_ok)
        trace = RunTrace(path=["start", via, "finish"], ok=ok)
        graph, stats, log = merge_run(graph, stats, log, trace)
    return graph, stats


def replay(runs: int = 60, noise: float = 0.1, seed: int = 7) -> ReplayResult:
    rng = random.Random(seed)
    # The agent picks the bad branch 60% of the time (a plausible prior); the
    # good branch succeeds at 1-noise, the bad one at noise.
    g, stats = simulate(
        diamond(),
        runs=runs,
        p_bad_branch=0.6,
        p_good_ok=1 - noise,
        p_bad_ok=noise,
        rng=rng,
    )
    g, _ = evolve_graph(g, stats, min_runs=3)
    dominant = dominant_path(g)
    prone_nodes = [n.id for n in g.nodes if n.fail_prone]
    prone_edges = [(e.src, e.dst) for e in g.edges if e.fail_prone]

    # Revision A (bad): drop the good branch. Runs against it fail at `noise`.
    before = RunStats(total=stats.total, ok=stats.ok)
    bad_rev = normalize_graph(
        {
            "nodes": [n.model_dump() for n in g.nodes if n.id != "good"],
            "edges": [e.model_dump() for e in g.edges if "good" not in (e.src, e.dst)],
        }
    )
    bad_rev = carry_stats(g, bad_rev)
    _, after_bad = simulate(
        bad_rev,
        runs=max(5, runs // 4),
        p_bad_branch=1.0,
        p_good_ok=1 - noise,
        p_bad_ok=noise,
        rng=rng,
    )
    bad_gate = gate_revision(before, after_bad)

    # Revision B (good): drop the bad branch. Runs against it succeed at 1-noise.
    good_rev = normalize_graph(
        {
            "nodes": [n.model_dump() for n in g.nodes if n.id != "bad"],
            "edges": [e.model_dump() for e in g.edges if "bad" not in (e.src, e.dst)],
        }
    )
    good_rev = carry_stats(g, good_rev)
    _, after_good = simulate(
        good_rev,
        runs=max(5, runs // 4),
        p_bad_branch=0.0,
        p_good_ok=1 - noise,
        p_bad_ok=noise,
        rng=rng,
    )
    good_gate = gate_revision(before, after_good)
    # Sequential gate, one run at a time: how fast does it decide?
    sprt_bad = runs_to_verdict(before, p_ok=noise, rng=rng)
    sprt_good = runs_to_verdict(before, p_ok=1 - noise, rng=rng)
    return ReplayResult(
        runs=runs,
        dominant=dominant,
        fail_prone_nodes=prone_nodes,
        fail_prone_edges=prone_edges,
        bad_gate=bad_gate,
        good_gate=good_gate,
        sprt_bad=sprt_bad,
        sprt_good=sprt_good,
    )


@dataclass
class UtilityResult:
    """Two procedures match one situation equally; the ledger says one works."""

    runs: int
    utility_good: float
    utility_bad: float
    first_run_good_leads: int  # runs folded before the good one ranks first at weight 1
    leads_at_weight_zero: bool  # whether the good one leads when the weight is 0 (it must not)


def utility_selection(
    runs: int = 40,
    p_good_ok: float = 0.8,
    p_bad_ok: float = 0.3,
    seed: int = 7,
    similarity_edge: float = 0.02,
    weight: float = 1.0,
) -> UtilityResult:
    """The utility ablation on loom's ledger: with the weight
    at 0 the more similar procedure keeps the top slot whatever the runs say;
    with a positive weight the one that succeeds overtakes it, and the number
    of runs that took is the price of the signal. The bad procedure is the
    more similar one by `similarity_edge`, so similarity alone always picks
    it. Pure: Bernoulli outcomes, no ClickHouse."""
    rng = random.Random(seed)
    good = RunStats()
    bad = RunStats()
    sim_good, sim_bad = 0.80, 0.80 + similarity_edge
    first_lead = 0
    for i in range(1, runs + 1):
        good = RunStats(total=good.total + 1, ok=good.ok + (1 if rng.random() < p_good_ok else 0))
        bad = RunStats(total=bad.total + 1, ok=bad.ok + (1 if rng.random() < p_bad_ok else 0))
        score_good = sim_good + weight * utility_from_stats(good.total, good.ok)
        score_bad = sim_bad + weight * utility_from_stats(bad.total, bad.ok)
        if first_lead == 0 and score_good > score_bad:
            first_lead = i
    leads_at_zero = sim_good + 0.0 * utility_from_stats(good.total, good.ok) > sim_bad
    return UtilityResult(
        runs=runs,
        utility_good=utility_from_stats(good.total, good.ok),
        utility_bad=utility_from_stats(bad.total, bad.ok),
        first_run_good_leads=first_lead,
        leads_at_weight_zero=leads_at_zero,
    )


def two_axis_negative_transfer() -> dict[str, Any]:
    """The goal-only ablation (a one-axis key can score below having no memory
    at all), in loom's terms: a procedure whose precondition matches strongly but whose goal points
    elsewhere, against one that fits both moderately. Situation-only ranks
    the wrong one first; the geometric mean of both axes ranks the right
    one first. Pure: fixed cosines, no embedder."""
    from server.memory.domain.procedure_evidence import two_axis_score

    one_axis = {"wrong_goal": two_axis_score(0.9, None), "right": two_axis_score(0.6, None)}
    two_axis = {"wrong_goal": two_axis_score(0.9, 0.1), "right": two_axis_score(0.6, 0.7)}
    return {
        "situation_only_first": max(one_axis, key=lambda k: one_axis[k]),
        "two_axis_first": max(two_axis, key=lambda k: two_axis[k]),
        "two_axis_scores": {k: round(v, 4) for k, v in two_axis.items()},
    }


def flag_rule_sweep(seeds: int = 400, runs: int = 60) -> list[dict[str, Any]]:
    """Point estimate vs lower confidence bound for the fail-prone flag
    . A diamond where the alternative branch is taken 20% of
        the time and the main branch succeeds 90%: when the alternative is as
        good as the main, every flag is false; when it is bad, the flag is right.
        Per rule: how many of `seeds` runs ever flagged the alternative within
        `runs` runs, how many still had it flagged at the end, and the median
        run of the first flag. Pure."""
    from server.memory.domain.procedure_graph import CONTRAST_MARGIN, MIN_CONTRAST_RUNS, _fail_prone

    def point_rule(n: float, fails: float, base: float) -> bool:
        if n < MIN_CONTRAST_RUNS:
            return False
        thr = base + CONTRAST_MARGIN
        return thr <= 1.0 and fails / n >= thr

    rows: list[dict[str, Any]] = []
    for alt_ok in (0.9, 0.8, 0.6, 0.3):
        row: dict[str, Any] = {"alt_ok": alt_ok, "fine": alt_ok >= 0.8}
        for name, rule in (("point", point_rule), ("lcb", _fail_prone)):
            ever = sticky = 0
            firsts: list[int] = []
            for seed in range(seeds):
                rng = random.Random(seed)
                alt_runs = alt_fails = tot = ok = 0
                flagged = final = False
                first: int | None = None
                for i in range(1, runs + 1):
                    alt = rng.random() < 0.2
                    success = rng.random() < (alt_ok if alt else 0.9)
                    tot += 1
                    ok += int(success)
                    if alt:
                        alt_runs += 1
                        alt_fails += int(not success)
                    f = rule(alt_runs, alt_fails, (tot - ok) / tot)
                    if f and first is None:
                        first = i
                    flagged |= f
                    final = f
                ever += int(flagged)
                sticky += int(final)
                if first is not None:
                    firsts.append(first)
            firsts.sort()
            row[name] = {
                "ever": ever,
                "at_end": sticky,
                "p50_first": firsts[len(firsts) // 2] if firsts else None,
            }
        rows.append(row)
    return rows


def cost_tie_break() -> dict[str, Any]:
    """Two revisions with equal success, one taking 5 steps and
    the other 9. The success gate accepts both; the cost check reverts the
    dearer one and keeps the cheaper, and says nothing when a side has too
    few costed successes. Pure."""
    from server.memory.domain.procedure_graph import RevisionCost, cost_verdict

    lean = RevisionCost(samples=6, p50_steps=5, p50_duration_ms=0, p50_tokens=0)
    heavy = RevisionCost(samples=6, p50_steps=9, p50_duration_ms=0, p50_tokens=0)
    thin = RevisionCost(samples=2, p50_steps=9)
    return {
        "heavy_after_lean": cost_verdict(lean, heavy),
        "lean_after_heavy": cost_verdict(heavy, lean),
        "thin_evidence": cost_verdict(lean, thin),
        "within_tolerance": cost_verdict(lean, RevisionCost(samples=6, p50_steps=6)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", type=int, default=60)
    ap.add_argument("--noise", type=float, default=0.1)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    r = replay(runs=a.runs, noise=a.noise, seed=a.seed)
    print(f"runs={r.runs} noise={a.noise} seed={a.seed}")
    print(f"dominant path      : {' -> '.join(r.dominant)}")
    print(f"fail-prone nodes   : {r.fail_prone_nodes}")
    print(f"fail-prone edges   : {r.fail_prone_edges}")
    print(f"gate: drop good    : {r.bad_gate}   (expected revert)")
    print(f"gate: drop bad     : {r.good_gate}  (expected accept)")
    print(f"sprt: drop good    : {r.sprt_bad[0]} after {r.sprt_bad[1]} runs")
    print(f"sprt: drop bad     : {r.sprt_good[0]} after {r.sprt_good[1]} runs")
    ax = two_axis_negative_transfer()
    ct = cost_tie_break()
    print(
        f"cost tie-break     : 9 steps after 5 -> {ct['heavy_after_lean']}, "
        f"5 after 9 -> {ct['lean_after_heavy']}, thin evidence -> {ct['thin_evidence']}, "
        f"6 after 5 -> {ct['within_tolerance']}"
    )
    for row in flag_rule_sweep():
        kind = "fine, any flag false" if row["fine"] else "bad, flag is right "
        pt, lb = row["point"], row["lcb"]
        print(
            f"flag rule alt_ok={row['alt_ok']} ({kind}): "
            f"point ever {pt['ever']}/400 end {pt['at_end']} p50 {pt['p50_first']}"
            f" | lcb ever {lb['ever']}/400 end {lb['at_end']} p50 {lb['p50_first']}"
        )
    print(
        f"two-axis           : situation-only puts '{ax['situation_only_first']}' first, "
        f"both axes put '{ax['two_axis_first']}' first {ax['two_axis_scores']}"
    )
    ok = (
        r.dominant == ["start", "good", "finish"]
        and "bad" in r.fail_prone_nodes
        and "good" not in r.fail_prone_nodes
        and r.bad_gate == "revert"
        and r.good_gate == "accept"
        and r.sprt_bad[0] == "revert"
        and r.sprt_good[0] == "accept"
        and ax["situation_only_first"] == "wrong_goal"
        and ax["two_axis_first"] == "right"
        and ct
        == {
            "heavy_after_lean": "costlier",
            "lean_after_heavy": "cheaper",
            "thin_evidence": None,
            "within_tolerance": None,
        }
    )
    u = utility_selection(runs=a.runs, seed=a.seed)
    print(
        f"utility: good {u.utility_good:+.3f} bad {u.utility_bad:+.3f}; good leads from run "
        f"{u.first_run_good_leads} at weight 1; leads at weight 0: {u.leads_at_weight_zero}"
    )
    ok = ok and u.first_run_good_leads > 0 and not u.leads_at_weight_zero
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
