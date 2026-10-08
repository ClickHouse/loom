"""Per-failed-slot loss ledger for MemoryArena group travel.

For every constraint slot an arm fails, name the constraint sentence, the
travelers it cites, the gold and the agent's value, whether each cited
traveler's trace (whole-trace arms) or rows (extraction arms, owner label)
were in the retrieved window, whether the referenced value was in the window,
whether the cited traveler's OWN stored plan already deviated from gold
(an inherited miss the memory could not have fixed), and whether the
long-context arm passed the same slot (the agent floor). Then classify:

  agent floor          long context fails the slot too
  own constraint       no traveler cited; the agent's own search failed
  inherited            the cited traveler's stored value differs from gold
  retrieval miss       the cited traveler's trace / rows are not in the window
  right-row miss       the actor is in the window, the referenced value is not
  reasoning            everything needed is in the window

usage: python bench/memoryarena/travel/ledger.py --gym <MemoryArena checkout>
           [--json out.json] ARM [ARM ...]
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

DAYW = {"first": 1, "second": 2, "third": 3, "1st": 1, "2nd": 2, "3rd": 3}
SLOTW = {
    "breakfast": ["breakfast"],
    "lunch": ["lunch"],
    "dinner": ["dinner"],
    "accommodation": ["accommodation", "stay", "room", "house", "lodging", "hotel", "apartment"],
}
STOP = {
    "I",
    "For",
    "On",
    "Day",
    "The",
    "My",
    "Please",
    "Also",
    "And",
    "In",
    "If",
    "It",
    "Craft",
    "Can",
    "Design",
    "Help",
}
NUM = re.compile(r"\$|%|rating|within|more than|less than|cheaper|expensive|rule|room type", re.I)
SLOT_RE = "breakfast|lunch|dinner|accommodation|stay|place"


def _day_of(sent: str) -> int | None:
    m = re.search(r"\b(first|second|third|1st|2nd|3rd)[- ]day", sent, re.I)
    if m:
        return DAYW[m.group(1).lower()]
    m = re.search(r"\bday (\d)", sent, re.I)
    return int(m.group(1)) if m else None


def _sentence_for(query: str, day: int, slot: str) -> str:
    best = ""
    for s in re.split(r"(?<=[.!?])\s+|\n+", query):
        if not any(w in s.lower() for w in SLOTW[slot]):
            continue
        d = _day_of(s)
        if d == day:
            return s.strip()
        if d is None and not best:
            best = s.strip()
    return best


def _reference(sent: str, name: str, day: int, slot: str) -> tuple[int, str]:
    """The (day, slot) a clause cites for `name`: "Emma's third-day dinner" →
    (3, dinner); "Zoey's third-day lunch" → (3, lunch); a bare "join Noah" →
    the constraint's own day and slot."""
    m = re.search(rf"{name}'s (first|second|third|1st|2nd|3rd)[- ]day (?:{SLOT_RE})?", sent, re.I)
    if not m:
        return day, slot
    rday = DAYW[m.group(1).lower()]
    tail = sent[m.end(1) :].lower()
    for cand in ("breakfast", "lunch", "dinner"):
        if cand in tail[:40]:
            return rday, cand
    if any(w in tail[:40] for w in ("accommodation", "stay", "place", "room")):
        return rday, "accommodation"
    return rday, slot


def _venue(v: object) -> str:
    return str(v or "-").split(",")[0].strip()


def _norm(s: str) -> str:
    """Whitespace-collapsed lowercase: a gold venue and the trace's copy of it
    can differ by a double space or a trailing mark."""
    return re.sub(r"\s+", " ", s.lower()).strip()


def _value_in(venue: str, ctx: str) -> bool:
    v = _norm(venue)
    return bool(v) and (v in _norm(ctx) or v[:20] in _norm(ctx))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gym", required=True)
    ap.add_argument("--json", default="")
    ap.add_argument("arms", nargs="+")
    a = ap.parse_args()
    sys.path.insert(0, a.gym)
    os.chdir(a.gym)
    from env.env_systems.travel_env import _parse_person_plan_from_result
    from env.env_systems.travel_planner_env import eval as J
    from env.env_systems.travel_planner_env.data_loader import load_travel_data

    gt, base, _ = J.load_ground_truth()
    base_name = {r["id"]: r["base_person"]["name"] for r in load_travel_data()}

    def val(plan, day, slot):
        d = J.get_day(plan, day) if plan else None
        return (d or {}).get(slot, "-")

    def load_arm(arm):
        # every saved group, not a fixed four — a scaled run has twenty-four
        out = {}
        for f in sorted(glob.glob(f"results/travel/{arm}/generated_plan_*.json")):
            g = int(f.rsplit("_", 1)[-1].split(".")[0])
            d = json.load(open(f))
            for r in d["rounds"]:
                out[(g, r["round"])] = r
        return out

    lc = load_arm("long_context")
    ledger = []
    for arm in a.arms:
        rounds = load_arm(arm)
        verbatim = "extract" not in arm
        for (g, pi), r in sorted(rounds.items()):
            name = r["name"]
            plan = _parse_person_plan_from_result(r["result"], name)
            gold = gt.get((g, pi))
            group = {rr["name"]: k[1] for k, rr in rounds.items() if k[0] == g}
            ctx = r.get("memory_context") or ""
            for day, slot in sorted(J.find_constraint_slots(gt, base, g, pi)):
                if J.check_slot_pass(gold, plan, day, slot):
                    continue
                sent = _sentence_for(r["query"], day, slot)
                words = re.findall(r"\b([A-Z][a-z]+)\b", sent)
                cited = list(dict.fromkeys(w for w in words if w not in STOP and w != name))
                refs = []
                for c in cited:
                    rday, rslot = _reference(sent, c, day, slot)
                    if c == base_name.get(g):
                        gplan, stored = base.get(g), base.get(g)
                    elif c in group:
                        gplan = gt.get((g, group[c]))
                        rr = rounds.get((g, group[c]))
                        stored = _parse_person_plan_from_result(rr["result"], c) if rr else None
                    else:
                        continue
                    rv = val(gplan, rday, rslot)
                    sv = val(stored, rday, rslot) if stored else "-"
                    deviates = sv != "-" and rv != "-" and J.similarity(rv, sv) < J.SIM_TH
                    if verbatim:
                        present = f'"name": "{c}"' in ctx
                    else:
                        present = f"[{c.lower()}]" in ctx or c in ctx
                    refs.append(
                        {
                            "name": c,
                            "ref_day": rday,
                            "ref_slot": rslot,
                            "gold_value": rv,
                            "stored_value": sv,
                            "upstream_deviates": bool(deviates),
                            "in_window": present,
                            "ref_in_window": (_value_in(_venue(rv), ctx) if rv != "-" else None),
                        }
                    )
                lc_r = lc.get((g, pi))
                lc_ok = None
                if lc_r:
                    lc_plan = _parse_person_plan_from_result(lc_r["result"], name)
                    lc_ok = J.check_slot_pass(gold, lc_plan, day, slot)
                if lc_ok is False:
                    cls = "agent floor"
                elif not refs:
                    cls = "own constraint"
                elif any(x["upstream_deviates"] for x in refs):
                    cls = "inherited"
                elif any(not x["in_window"] for x in refs):
                    cls = "retrieval miss"
                elif any(x["ref_in_window"] is False for x in refs):
                    cls = "right-row miss"
                else:
                    cls = "reasoning"
                ledger.append(
                    {
                        "arm": arm,
                        "group": g,
                        "round": pi,
                        "name": name,
                        "day": day,
                        "slot": slot,
                        "constraint": sent,
                        "numeric": bool(NUM.search(sent)),
                        "gold": str(val(gold, day, slot)),
                        "agent": str(val(plan, day, slot)),
                        "cited": refs,
                        "long_context_pass": lc_ok,
                        "ctx_chars": len(ctx),
                        "class": cls,
                    }
                )
    if a.json:
        json.dump(ledger, open(a.json, "w"), indent=1)
    by = collections.defaultdict(collections.Counter)
    for row in ledger:
        by[row["arm"]][row["class"]] += 1
    for arm in a.arms:
        c = by[arm]
        print(f"{arm}: {sum(c.values())} failed slots")
        for k, v in c.most_common():
            print(f"  {v:3d}  {k}")
    print()
    for row in ledger:
        refs = "; ".join(
            f"{x['name']}→{_venue(x['gold_value'])[:20]}"
            + (" (stored " + _venue(x["stored_value"])[:16] + ")" if x["upstream_deviates"] else "")
            + ("" if x["in_window"] else " ABSENT")
            for x in row["cited"]
        )
        who = f"g{row['group']} {row['name']:9s} d{row['day']} {row['slot']:13s}"
        head = f"{row['arm'][-14:]:14s} {who}"
        vals = f"gold={_venue(row['gold'])[:22]:22s} agent={_venue(row['agent'])[:22]:22s}"
        print(f"{head} {row['class']:15s} | {vals} | {refs} | {row['constraint'][:90]}")


if __name__ == "__main__":
    main()
