"""Calibrate the LoCoMo judge once: TPR / FPR on answers of known truth.

The judge is the instrument every accuracy gate reads through, and it is
quoted without an error rate. Estimating the judge's true- and false-positive
rates on a small labelled set lets the gate correct the test's threshold for
them. There is no hand-labelled LoCoMo set here, so the
labelled set is CONSTRUCTED from the gold answers:

* known-correct: the item's own gold answer, written as the reader would
  ("Based on their conversations, <gold>.") — a judge that fails these has
  a false-negative rate;
* known-wrong: another item's gold from the same category, same shape —
  the topical-but-wrong answer a lenient judge waves through.

Adversarial items (no gold) are excluded. The judge prompt and model are
`bench/locomo_qa.py`'s own, so the rates describe the instrument the bench
runs. Output JSON feeds `--judge-calibration` on every gate
(`bench/_gate.py`), which de-attenuates a paired delta by Youden's
J = TPR − FPR and widens its interval by the calibration variance.

    python bench/judge_calibration.py --dataset bench/datasets/locomo10.json \\
        --n 100 --judge-model gpt-4.1 --out bench/judge_calibration.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.locomo_qa import (
    _DEFAULT_JUDGE_MODEL,
    _JUDGE_TEMPLATE,
    _chat,
    gold_answer,
    parse_judge_label,
)

POSITIVE_SHAPE = "Based on their conversations, {gold}."


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def build_probes(
    data: list[dict[str, Any]], n: int, seed: int, *, categories: tuple[int, ...] = (1, 2, 3, 4)
) -> list[dict[str, Any]]:
    """`n` known-correct and `n` known-wrong probes, deterministic under
    `seed`. A wrong answer is another item's gold from the SAME category
    whose normalised text differs from the item's own gold (and from a
    trivially shared token like a bare year), so the label is wrong by
    construction and not by accident."""
    items: list[dict[str, Any]] = []
    for conv in data:
        for i, q in enumerate(conv.get("qa") or []):
            cat = int(q.get("category", 0))
            gold = gold_answer(q)
            if cat in categories and gold:
                items.append(
                    {
                        "sample_id": str(conv.get("sample_id", "")),
                        "question_index": i,
                        "category": cat,
                        "question": str(q.get("question", "")),
                        "gold": gold,
                    }
                )
    rng = random.Random(seed)
    rng.shuffle(items)
    chosen = items[:n]
    by_cat: dict[int, list[dict[str, Any]]] = {}
    for it in items:
        by_cat.setdefault(it["category"], []).append(it)
    probes: list[dict[str, Any]] = []
    for it in chosen:
        probes.append(
            {**it, "truth": "CORRECT", "generated": POSITIVE_SHAPE.format(gold=it["gold"])}
        )
        pool = [
            o
            for o in by_cat[it["category"]]
            if _norm(o["gold"]) != _norm(it["gold"])
            and _norm(o["gold"]) not in _norm(it["gold"])
            and _norm(it["gold"]) not in _norm(o["gold"])
        ]
        other = rng.choice(pool) if pool else None
        if other is None:
            continue
        probes.append(
            {
                **it,
                "truth": "WRONG",
                "generated": POSITIVE_SHAPE.format(gold=other["gold"]),
                "swapped_from": f"{other['sample_id']}:{other['question_index']}",
            }
        )
    return probes


def summarize(probes: list[dict[str, Any]], judge_model: str) -> dict[str, Any]:
    """TPR = judged CORRECT among known-correct; FPR = judged CORRECT among
    known-wrong; errors (unparseable replies) are excluded from both."""
    pos = [p for p in probes if p["truth"] == "CORRECT" and p.get("label") in ("CORRECT", "WRONG")]
    neg = [p for p in probes if p["truth"] == "WRONG" and p.get("label") in ("CORRECT", "WRONG")]
    tp = sum(p["label"] == "CORRECT" for p in pos)
    fp = sum(p["label"] == "CORRECT" for p in neg)
    tpr = tp / len(pos) if pos else 0.0
    fpr = fp / len(neg) if neg else 0.0
    by_cat: dict[str, dict[str, Any]] = {}
    for p in pos + neg:
        c = by_cat.setdefault(str(p["category"]), {"pos": 0, "tp": 0, "neg": 0, "fp": 0})
        if p["truth"] == "CORRECT":
            c["pos"] += 1
            c["tp"] += p["label"] == "CORRECT"
        else:
            c["neg"] += 1
            c["fp"] += p["label"] == "CORRECT"
    for c in by_cat.values():
        c["tpr"] = round(c["tp"] / c["pos"], 3) if c["pos"] else None
        c["fpr"] = round(c["fp"] / c["neg"], 3) if c["neg"] else None
    return {
        "judge_model": judge_model,
        "tpr": round(tpr, 4),
        "fpr": round(fpr, 4),
        "youden": round(tpr - fpr, 4),
        "n_pos": len(pos),
        "n_neg": len(neg),
        "errors": sum(1 for p in probes if p.get("label") not in ("CORRECT", "WRONG")),
        "by_category": by_cat,
        "false_negatives": [
            {"question": p["question"], "gold": p["gold"], "generated": p["generated"]}
            for p in pos
            if p["label"] == "WRONG"
        ][:10],
        "false_positives": [
            {"question": p["question"], "gold": p["gold"], "generated": p["generated"]}
            for p in neg
            if p["label"] == "CORRECT"
        ][:10],
    }


async def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("BENCH_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY is required for the judge", file=sys.stderr)
        return 2
    data = json.loads(Path(args.dataset).read_text())
    probes = build_probes(data, int(args.n), int(args.seed))
    usage = {"tin": 0, "tout": 0, "calls": 0}
    sem = asyncio.Semaphore(max(1, int(args.concurrency)))
    async with httpx.AsyncClient() as client:

        async def _one(p: dict[str, Any]) -> None:
            prompt = _JUDGE_TEMPLATE.format(
                question=p["question"], gold=p["gold"], generated=p["generated"]
            )
            async with sem:
                try:
                    reply = await _chat(
                        client, args.judge_model, prompt, api_key, max_tokens=20, usage=usage
                    )
                    p["label"] = parse_judge_label(reply) or "ERROR"
                except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
                    p["label"] = "ERROR"
                    p["error"] = str(e)[:120]

        await asyncio.gather(*(_one(p) for p in probes))
    summary = summarize(probes, args.judge_model)
    summary.update(
        {
            "dataset": str(args.dataset),
            "seed": int(args.seed),
            "positive_shape": POSITIVE_SHAPE,
            "negative_construction": "another item's gold from the same category",
            "judge_tokens_in": usage["tin"],
            "judge_tokens_out": usage["tout"],
            "judge_calls": usage["calls"],
        }
    )
    if args.probes_out:
        Path(args.probes_out).write_text("\n".join(json.dumps(p) for p in probes) + "\n")
    if not summary["n_pos"] or not summary["n_neg"]:
        # TPR 0 / FPR 0 from zero judged probes reads as a calibration (J = 0);
        # write none, so no gate picks it up.
        print(
            f"no calibration: {summary['n_pos']} known-correct and {summary['n_neg']} "
            f"known-wrong probes judged ({summary['errors']} errors); {args.out} not written",
            file=sys.stderr,
        )
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=1))
    print(
        f"judge {args.judge_model}: TPR {summary['tpr']} ({summary['n_pos']} known-correct)  "
        f"FPR {summary['fpr']} ({summary['n_neg']} known-wrong)  J {summary['youden']}  "
        f"errors {summary['errors']}  → {out}"
    )
    for cat, c in sorted(summary["by_category"].items()):
        print(f"  category {cat}: TPR {c['tpr']} ({c['pos']})  FPR {c['fpr']} ({c['neg']})")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dataset", required=True, help="path to locomo10.json")
    p.add_argument("--n", type=int, default=100, help="known-correct probes (and as many wrong)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--judge-model", default=_DEFAULT_JUDGE_MODEL)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--out", default="bench/judge_calibration.json")
    p.add_argument("--probes-out", default="", help="write every probe + label as JSONL")
    return asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
