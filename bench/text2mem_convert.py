"""Convert the public Text2Mem benchmark (MemTensor/text2mem
`bench/data/benchmark/benchmark.jsonl`, 2,947 samples) into op-planning tasks
for bench/op_planning.py — planning layer only.

Each Text2Mem sample carries a natural-language instruction and a gold
`schema_list` of verb objects. Verbs map onto loom's tool surface where a
counterpart exists; the rest are recorded as GAP so the converted set says
what loom cannot express instead of silently dropping it:

    Encode                 → loom_memory_set                 (memory_key, value)
    Retrieve               → loom_search                     (query)
    Summarize              → loom_memory_ask                 (query)
    Delete    ids          → loom_memory_delete              (memory_key)
    Delete    filter/search→ loom_memory_apply action=delete (target, action, reason, limit)
    Label                  → loom_memory_apply action=label  (+ label)
    Demote                 → loom_memory_apply action=demote (+ demote)
    Expire                 → loom_memory_apply action=expire (+ expire)
    Lock      ids (one)    → loom_memory_lock                (memory_key, reason)
    Lock      filter/search→ loom_memory_apply action=lock   (+ lock)
    Update    ids (one)    → loom_memory_update_by_id        (memory_id, value)
    Update    filter/search→ GAP  (loom has no wide value rewrite by design)
    Promote                → GAP  (loom's promote is a sharing lifecycle; salience up is pin)
    Merge, Split           → GAP  (consolidation is an admin sweep; no split on the memory plane)

The Text2Mem SQL assertions are SQLite-dialect and target its prototype
schema, so they are not converted; execution scoring stays with the
loom-native fixture.

Usage:
  python bench/text2mem_convert.py /path/to/benchmark.jsonl --lang en \
      --out bench/datasets/op_planning_text2mem_en.jsonl
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

APPLY_REQ = ["target", "action", "reason", "limit"]


def _target_kind(step: dict[str, Any]) -> str:
    t = step.get("target") or {}
    if t.get("ids"):
        return "ids"
    if t.get("filter"):
        return "filter"
    if t.get("search"):
        return "search"
    if t.get("all"):
        return "all"
    return "none"


def convert_step(step: dict[str, Any]) -> dict[str, Any] | None:
    """One Text2Mem verb object → one gold step, or None for a GAP."""
    op = step.get("op")
    kind = _target_kind(step)
    if op == "Encode":
        return {"tool": "loom_memory_set", "required": ["memory_key", "value"]}
    if op == "Retrieve":
        return {"tool": "loom_search", "required": ["query"]}
    if op == "Summarize":
        return {"tool": "loom_memory_ask", "required": ["query"]}
    if op == "Delete":
        if kind == "ids":
            return {"tool": "loom_memory_delete", "required": ["memory_key"]}
        return {"tool": "loom_memory_apply", "required": APPLY_REQ, "args": {"action": "delete"}}
    if op == "Label":
        return {
            "tool": "loom_memory_apply",
            "required": APPLY_REQ + ["label"],
            "args": {"action": "label"},
        }
    if op == "Demote":
        return {
            "tool": "loom_memory_apply",
            "required": APPLY_REQ + ["demote"],
            "args": {"action": "demote"},
        }
    if op == "Expire":
        return {
            "tool": "loom_memory_apply",
            "required": APPLY_REQ + ["expire"],
            "args": {"action": "expire"},
        }
    if op == "Lock":
        ids = (step.get("target") or {}).get("ids")
        if kind == "ids" and (isinstance(ids, str) or len(ids) == 1):
            return {"tool": "loom_memory_lock", "required": ["memory_key", "reason"]}
        return {
            "tool": "loom_memory_apply",
            "required": APPLY_REQ + ["lock"],
            "args": {"action": "lock"},
        }
    if op == "Update":
        ids = (step.get("target") or {}).get("ids")
        if kind == "ids" and (isinstance(ids, str) or len(ids) == 1):
            return {"tool": "loom_memory_update_by_id", "required": ["memory_id", "value"]}
        return None
    return None  # Promote, Merge, Split, unknown


def convert(
    samples: list[dict[str, Any]], lang: str = "en"
) -> tuple[list[dict[str, Any]], Counter[str]]:
    tasks: list[dict[str, Any]] = []
    gaps: Counter[str] = Counter()
    for s in samples:
        if s.get("class", {}).get("lang") != lang:
            continue
        prompt = (s.get("nl") or {}).get(lang) or ""
        if not prompt.strip():
            continue
        gold = []
        gap = None
        for step in s.get("schema_list") or []:
            g = convert_step(step)
            if g is None:
                gap = step.get("op", "?")
                break
            gold.append(g)
        if gap or not gold:
            gaps[gap or "empty"] += 1
            continue
        tasks.append(
            {
                "id": f"t2m-{s['id']}",
                "class": {
                    "instruction_type": s["class"]["instruction_type"],
                    "structure": s["class"]["structure"],
                    "source": "text2mem",
                },
                "prompt": prompt,
                "gold": gold,
            }
        )
    return tasks, gaps


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("benchmark_jsonl")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--out", default="bench/datasets/op_planning_text2mem_en.jsonl")
    args = ap.parse_args()
    lines = Path(args.benchmark_jsonl).read_text().splitlines()
    samples = [json.loads(line) for line in lines if line.strip()]
    tasks, gaps = convert(samples, args.lang)
    Path(args.out).write_text("\n".join(json.dumps(t, ensure_ascii=False) for t in tasks) + "\n")
    by = Counter(g["tool"] for t in tasks for g in t["gold"])
    print(f"{len(tasks)} tasks written to {args.out}; gold tools: {dict(by)}; gaps: {dict(gaps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
