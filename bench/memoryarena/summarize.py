"""Summarise MemoryArena result files the way the benchmark reports them.

Reads <results>/<arm>/<task>/result.jsonl (one row per subtask: is_correct,
time, query_id) and prints, per arm: task success rate (every subtask of
the task correct — SR), progress score (mean fraction of
subtasks correct — PS), success at subtask depth k (SR@k: share of tasks
whose k-th subtask was correct), and seconds per subtask. Pure; unit-tested.

    python bench/memoryarena/summarize.py --results scratch/memoryarena/results/json/math
"""

from __future__ import annotations

import argparse
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArmSummary:
    arm: str
    tasks: int = 0
    subtasks: int = 0
    success: int = 0
    progress_sum: float = 0.0
    seconds_sum: float = 0.0
    at_k_hit: dict[int, int] = field(default_factory=dict)
    at_k_n: dict[int, int] = field(default_factory=dict)
    # (task, query_id) -> correct, for the cross-arm floor
    per_subtask: dict[tuple[str, int], bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        n = max(1, self.tasks)
        ks = sorted(self.at_k_n)
        return {
            "arm": self.arm,
            "tasks": self.tasks,
            "subtasks": self.subtasks,
            "SR": round(self.success / n, 3),
            "PS": round(self.progress_sum / n, 3),
            "SR@k": {k: round(self.at_k_hit.get(k, 0) / self.at_k_n[k], 3) for k in ks},
            "s_per_subtask": round(self.seconds_sum / max(1, self.subtasks), 1),
        }


def summarise_task(rows: list[dict[str, Any]]) -> tuple[bool, float, list[bool], float]:
    """(all correct, fraction correct, per-depth correctness, total seconds)
    for one task's rows, ordered by query_id."""
    rows = sorted(rows, key=lambda r: int(r.get("query_id", 0)))
    ok = [bool(r.get("is_correct")) for r in rows]
    return (
        all(ok) and bool(ok),
        (sum(ok) / len(ok)) if ok else 0.0,
        ok,
        sum(float(r.get("time") or 0.0) for r in rows),
    )


def summarise(results: pathlib.Path) -> list[ArmSummary]:
    out: list[ArmSummary] = []
    for arm_dir in sorted(p for p in results.iterdir() if p.is_dir()):
        s = ArmSummary(arm=arm_dir.name)
        for f in sorted(arm_dir.glob("*/result.jsonl")):
            rows = []
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a file mid-write by a running arm: skip the torn line
            if not rows:
                continue
            success, progress, per_depth, secs = summarise_task(rows)
            s.tasks += 1
            s.subtasks += len(rows)
            s.success += int(success)
            s.progress_sum += progress
            s.seconds_sum += secs
            for k, hit in enumerate(per_depth, start=1):
                s.at_k_n[k] = s.at_k_n.get(k, 0) + 1
                s.at_k_hit[k] = s.at_k_hit.get(k, 0) + int(hit)
            for r in rows:
                s.per_subtask[(f.parent.name, int(r.get("query_id", 0)))] = bool(
                    r.get("is_correct")
                )
        if s.tasks:
            out.append(s)
    return out


def floor(arms: list[ArmSummary]) -> dict[str, Any]:
    """The memory-independent floor: subtasks every arm got wrong are the task
    agent's failures, not the memory system's. Restrict to subtasks present in
    EVERY arm; report the floor and each arm's accuracy on the remaining
    memory-sensitive subtasks — the denominator a memory delta should use
    (MemoryArena formal reasoning: 52 of 80 subtasks failed under every arm)."""
    if len(arms) < 2:
        return {}
    common = set.intersection(*[set(s.per_subtask) for s in arms])
    if not common:
        return {}
    all_wrong = {k for k in common if not any(s.per_subtask[k] for s in arms)}
    sensitive = sorted(common - all_wrong)
    return {
        "common_subtasks": len(common),
        "all_arms_wrong": len(all_wrong),
        "floor_share": round(len(all_wrong) / len(common), 3),
        "memory_sensitive": len(sensitive),
        "accuracy_on_sensitive": {
            s.arm: round(sum(s.per_subtask[k] for k in sensitive) / len(sensitive), 3) for s in arms
        }
        if sensitive
        else {},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--results", required=True, help="directory holding <arm>/<task>/result.jsonl")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-k", type=int, default=6)
    a = ap.parse_args(argv)
    arms = summarise(pathlib.Path(a.results))
    if a.json:
        print(json.dumps({"arms": [s.as_dict() for s in arms], "floor": floor(arms)}, indent=2))
        return 0
    ks = range(1, a.max_k + 1)
    head = " ".join(f"@{k:<5}" for k in ks)
    print(f"{'arm':26} {'tasks':>5} {'subt':>5} {'SR':>6} {'PS':>6} {head}  s/subtask")
    for s in arms:
        d = s.as_dict()
        atk = " ".join(f"{d['SR@k'][k]:6.3f}" if k in d["SR@k"] else "     -" for k in ks)
        print(
            f"{d['arm']:26} {d['tasks']:5d} {d['subtasks']:5d} {d['SR']:6.3f} "
            f"{d['PS']:6.3f} {atk}  {d['s_per_subtask']:.1f}"
        )
    fl = floor(arms)
    if fl:
        print(
            f"\nfloor: {fl['all_arms_wrong']}/{fl['common_subtasks']} common subtasks "
            f"wrong under every arm ({fl['floor_share']:.0%}); accuracy on the "
            f"{fl['memory_sensitive']} memory-sensitive ones:"
        )
        for arm, acc in fl["accuracy_on_sensitive"].items():
            print(f"  {arm:26} {acc:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
