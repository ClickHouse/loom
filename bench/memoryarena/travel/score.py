"""Score MemoryArena group-travel arms offline, in the benchmark's shape.

Reads the gym's ``results/travel/<arm>/generated_plan_<group>.json`` files as
written by the patched runner (``run_travel.patch`` adds a ``rounds`` list
with per-person reward, steps, seconds and the retrieved memory context) and
scores every person with the gym's own judge functions:

* PS  — full-plan pass: every slot of every day within ``SIM_TH`` of the gold
  plan (the benchmark's progress score; near zero for every system).
* sPS — constraint-slot pass: only the slots that differ from the base
  traveler's plan, i.e. the ones the joiner's query constrained (the benchmark's
  soft progress score, the informative one on this environment).

A loom-arm round whose retrieved context is empty is a memory outage (a 5xx
from storage that the plugin fails open on), not a retrieval result; it is
excluded and counted.

    python bench/memoryarena/travel/score.py --gym <MemoryArena checkout>
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
from typing import Any

EMPTY_CONTEXT_CHARS = 50


def faithful_pass(
    judge: Any,
    query: str,
    name: str,
    day: int,
    slot: str,
    plan: Any,
    stored: dict[str, Any],
    base_plan: Any,
    base_person: str | None,
) -> bool:
    """True when the constraint for (day, slot) is a join / same-place clause
    citing a traveler whose stored value at the cited (day, slot) the agent
    matched. Numeric relations ("$15 more than", "within 10%") are not judged
    here; they keep the gold verdict."""
    from ledger import NUM, STOP, _reference, _sentence_for

    sent = _sentence_for(query, day, slot)
    if not sent or NUM.search(sent):
        return False
    cited = [w for w in re.findall(r"\b([A-Z][a-z]+)\b", sent) if w not in STOP and w != name]
    own = (judge.get_day(plan, day) or {}).get(slot, "-") if plan else "-"
    for c in dict.fromkeys(cited):
        rday, rslot = _reference(sent, c, day, slot)
        ref_plan = base_plan if c == base_person else stored.get(c)
        if not ref_plan:
            continue
        ref = (judge.get_day(ref_plan, rday) or {}).get(rslot, "-")
        if ref != "-" and own != "-" and judge.similarity(ref, own) >= judge.SIM_TH:
            return True
    return False


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_rows(gym: str) -> tuple[list[dict[str, Any]], list[tuple[str, int, int]]]:
    sys.path.insert(0, gym)
    from env.env_systems.travel_env import _parse_person_plan_from_result
    from env.env_systems.travel_planner_env import eval as judge

    gt, base_plans, _ = judge.load_ground_truth()
    from env.env_systems.travel_planner_env.data_loader import load_travel_data

    base_name = {row["id"]: row["base_person"]["name"] for row in load_travel_data()}
    stored_plans: dict[str, dict[int, dict[str, Any]]] = {}
    root = os.path.join(gym, "results/travel")
    rows: list[dict[str, Any]] = []
    skipped: list[tuple[str, int, int]] = []
    for arm in sorted(os.listdir(root)):
        if arm == "logs":
            continue
        for f in sorted(glob.glob(f"{root}/{arm}/generated_plan_*.json")):
            group = int(f.split("_")[-1].split(".")[0])
            for r in json.load(open(f)).get("rounds", []):
                pidx, name = r["round"], r["name"]
                ctx = r.get("memory_context_chars") or 0
                if arm.startswith("loom") and ctx < EMPTY_CONTEXT_CHARS:
                    skipped.append((arm, group, pidx))
                    continue
                plan = _parse_person_plan_from_result(r["result"], name)
                gold = gt.get((group, pidx))
                slots = judge.find_constraint_slots(gt, base_plans, group, pidx)
                passed = sum(judge.check_slot_pass(gold, plan, day, slot) for day, slot in slots)
                # Memory-faithful pass: a join / same-place slot
                # also passes when the agent matched what the cited traveler
                # actually stored in this run, even where that value had already
                # drifted from gold. Gold scores the cascade; this scores the memory.
                stored = stored_plans.setdefault(arm, {}).setdefault(group, {})
                stored[name] = plan
                faithful_extra = 0
                for day, slot in slots:
                    if judge.check_slot_pass(gold, plan, day, slot):
                        continue
                    if faithful_pass(
                        judge,
                        r["query"],
                        name,
                        day,
                        slot,
                        plan,
                        stored,
                        base_plans.get(group),
                        base_name.get(group),
                    ):
                        faithful_extra += 1
                rows.append(
                    {
                        "arm": arm,
                        "group": group,
                        "round": pidx,
                        "name": name,
                        "full": judge.check_person_full_pass(gold, plan),
                        "c_pass": passed,
                        "c_total": len(slots),
                        "f_pass": passed + faithful_extra,
                        "steps": r.get("steps") or 0,
                        "seconds": r.get("seconds") or 0.0,
                        "ctx": ctx,
                    }
                )
    return rows, skipped


def report(rows: list[dict[str, Any]], skipped: list[tuple[str, int, int]]) -> str:
    by: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for r in rows:
        by[r["arm"]].append(r)
    out = [f"excluded {len(skipped)} loom rounds with empty memory context: {skipped[:6]}"]
    out.append(
        f"{'arm':28s} groups persons  PS(full)  sPS(slot)  fPS(faithful)  slots  steps  "
        "s/person  ctx_chars"
    )
    for arm, rs in by.items():
        groups = {r["group"] for r in rs}
        sps = [r["c_pass"] / r["c_total"] for r in rs if r["c_total"]]
        fps = [r.get("f_pass", r["c_pass"]) / r["c_total"] for r in rs if r["c_total"]]
        n = len(rs)
        out.append(
            f"{arm:28s} {len(groups):6d} {n:7d}  {sum(r['full'] for r in rs) / n:.3f}     "
            f"{sum(sps) / max(1, len(sps)):.3f}   {sum(fps) / max(1, len(fps)):.3f}          "
            f"{sum(r['c_total'] for r in rs):5d}  "
            f"{sum(r['steps'] for r in rs) / n:5.1f}  {sum(r['seconds'] for r in rs) / n:7.0f}  "
            f"{sum(r['ctx'] for r in rs) / n:9.0f}"
        )
    keysets = [{(r["group"], r["round"]) for r in rs} for rs in by.values()]
    common = set.intersection(*keysets) if keysets else set()
    out.append(f"\npaired on {len(common)} common persons — constraint-slot pass rate:")
    for arm, rs in by.items():
        sel = [r for r in rs if (r["group"], r["round"]) in common]
        p = sum(r["c_pass"] for r in sel)
        t = sum(r["c_total"] for r in sel)
        full = sum(r["full"] for r in sel)
        out.append(f"  {arm:28s} {p}/{t} = {p / max(1, t):.3f}  full-pass {full}/{len(sel)}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--gym", required=True, help="MemoryArena checkout holding results/travel")
    ap.add_argument("--json", help="also write the per-person rows here")
    a = ap.parse_args(argv)
    rows, skipped = load_rows(a.gym)
    if a.json:
        json.dump(rows, open(a.json, "w"))
    print(report(rows, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
