"""Cost-per-session A/B on a consumer's own traffic — the number a consumer
actually decides on.

`bench/clickgap_code_replay.py` measures what loom can see: resolution,
coverage, latency, tokens injected. Passing those gates does not show that
an agent got cheaper with the index on. Loom cannot see an agent's LLM
bill; the agent's invocation ledger can. This script consumes that ledger
and computes per-arm cost, duration and findings, paired by PR/issue where
both arms ran the same item, with bootstrap confidence intervals — so the
"neutral without the block, harmful with it" result surfaces before a
production night, not after.

Ledger format (JSONL, one row per agent session; produced by the customer):
  {"session_id": "…", "agent": "pr-analyst", "arm": "on"|"off",
   "item": "PR-118968",            # what was analysed; pairs on/off rows
   "cost_usd": 1.23, "duration_s": 412.0,
   "findings": 5,                   # confirmed findings (optional)
   "index_calls": 2,                # loom calls made in-session (optional)
   "greps": 11}                     # tree greps (optional)

Usage:
  bench/session_ab.py ledger.jsonl [--agent pr-analyst] [--min-pairs 4]
      [--max-cost-delta 0.10]  # gate: paired mean cost delta (on−off)/off

Exit 1 when the gate fails. Report only, never a judgement of the index: a
positive cost delta with unchanged findings means "keep this agent off",
which is a product answer the customer needs on day one.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class ArmStats:
    n: int
    cost_mean: float
    duration_mean: float
    findings_mean: float | None
    index_calls_mean: float | None
    greps_mean: float | None


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def arm_stats(rows: list[dict]) -> ArmStats:
    def col(k: str) -> list[float]:
        return [float(r[k]) for r in rows if r.get(k) is not None]

    return ArmStats(
        n=len(rows),
        cost_mean=_mean(col("cost_usd")) or 0.0,
        duration_mean=_mean(col("duration_s")) or 0.0,
        findings_mean=_mean(col("findings")),
        index_calls_mean=_mean(col("index_calls")),
        greps_mean=_mean(col("greps")),
    )


def paired_deltas(rows: list[dict], key: str) -> list[float]:
    """Relative (on − off) / off per item that has both arms; the mean of
    each arm's sessions when an item ran more than once per arm."""
    by_item: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"on": [], "off": []})
    for r in rows:
        if r.get("item") and r.get("arm") in ("on", "off") and r.get(key) is not None:
            by_item[str(r["item"])][r["arm"]].append(float(r[key]))
    out: list[float] = []
    for arms in by_item.values():
        if arms["on"] and arms["off"]:
            off = statistics.fmean(arms["off"])
            if off > 0:
                out.append((statistics.fmean(arms["on"]) - off) / off)
    return out


def bootstrap_ci(xs: list[float], *, iters: int = 2000, seed: int = 7) -> tuple[float, float]:
    if len(xs) < 2:
        v = xs[0] if xs else 0.0
        return (v, v)
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(xs, k=len(xs))) for _ in range(iters))
    return means[int(0.025 * iters)], means[int(0.975 * iters) - 1]


def load(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def report(
    rows: list[dict], *, agent: str = "", min_pairs: int = 4, max_cost_delta: float | None = None
) -> int:
    if agent:
        rows = [r for r in rows if r.get("agent") == agent]
    on = [r for r in rows if r.get("arm") == "on"]
    off = [r for r in rows if r.get("arm") == "off"]
    if not on or not off:
        print("need sessions in both arms (arm: on|off)", file=sys.stderr)
        return 2
    a_on, a_off = arm_stats(on), arm_stats(off)
    print("=== session_ab" + (f" — agent {agent}" if agent else ""))
    print(f"sessions: on={a_on.n} off={a_off.n}")

    def line(label: str, on_v: float | None, off_v: float | None, fmt: str) -> None:
        if on_v is None or off_v is None:
            return
        delta = (on_v - off_v) / off_v if off_v else float("nan")
        print(f"  {label:14} on={on_v:{fmt}}  off={off_v:{fmt}}  delta={delta:+.1%}")

    line("cost_usd", a_on.cost_mean, a_off.cost_mean, ".3f")
    line("duration_s", a_on.duration_mean, a_off.duration_mean, ".0f")
    line("findings", a_on.findings_mean, a_off.findings_mean, ".2f")
    line("index_calls", a_on.index_calls_mean, a_off.index_calls_mean, ".1f")
    line("greps", a_on.greps_mean, a_off.greps_mean, ".1f")

    pairs = paired_deltas(rows, "cost_usd")
    print(f"paired items (both arms): {len(pairs)}")
    problems: list[str] = []
    if len(pairs) < min_pairs:
        print(f"  fewer than {min_pairs} paired items — unpaired means above are indicative only")
    else:
        lo, hi = bootstrap_ci(pairs)
        m = statistics.fmean(pairs)
        print(f"  paired cost delta (on−off)/off: mean {m:+.1%}  95% CI [{lo:+.1%}, {hi:+.1%}]")
        d = paired_deltas(rows, "duration_s")
        if d:
            lo2, hi2 = bootstrap_ci(d)
            print(
                f"  paired duration delta:          mean {statistics.fmean(d):+.1%}  "
                f"95% CI [{lo2:+.1%}, {hi2:+.1%}]"
            )
        verdict = (
            "index is NEUTRAL for this agent (CI spans 0)"
            if lo <= 0 <= hi
            else (
                "index is CHEAPER for this agent"
                if hi < 0
                else "index is MORE EXPENSIVE for this agent"
            )
        )
        print(f"  verdict: {verdict}")
        if max_cost_delta is not None and m > max_cost_delta:
            problems.append(f"paired mean cost delta {m:+.1%} > {max_cost_delta:+.0%}")
    if problems:
        print("GATE FAILED: " + "; ".join(problems))
        return 1
    print("gate passed" if max_cost_delta is not None else "no gate requested")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("ledger")
    ap.add_argument("--agent", default="")
    ap.add_argument("--min-pairs", type=int, default=4)
    ap.add_argument("--max-cost-delta", type=float, default=None)
    args = ap.parse_args()
    return report(
        load(args.ledger),
        agent=args.agent,
        min_pairs=args.min_pairs,
        max_cost_delta=args.max_cost_delta,
    )


if __name__ == "__main__":
    sys.exit(main())
