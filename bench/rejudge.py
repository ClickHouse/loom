"""Re-grade a LongMemEval answers file with other judges, and say how far they agree.

`bench/longmemeval.py --answer-judge` writes `<out>.hyp.jsonl` (question_id,
hypothesis). This grades each hypothesis again with every `--judge` model,
using the official per-question-type LongMemEval prompt (the same call the
harness makes, through bench/judges.py, so a `claude*` judge works as well as a
GPT one), and prints:

* accuracy per judge, overall and per question type;
* for every pair of judges, raw agreement and Cohen's kappa over the items both
  graded, plus the items they split on.

Use it to put a current judge beside the official one without paying for ingest
or answers again, and to check that a score is not one judge's lean: two judges
from different model families that agree on ~95% of items (kappa >= 0.85) make
the accuracy a property of the answers.

    OPENAI_API_KEY=... ANTHROPIC_API_KEY=... python -m bench.rejudge \\
        --hyp <out>.hyp.jsonl \\
        --dataset bench/datasets/longmemeval_s_cleaned.json \\
        --judge gpt-4o-2024-08-06 --judge gpt-6.1-sol --judge claude-opus-5-5 \\
        --out out/rejudge.json
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from bench.longmemeval import _llm_judge


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's kappa of two binary labelings of the same items (1.0 = perfect,
    0.0 = chance). Degenerate marginals with full agreement count as 1.0."""
    n = len(a)
    if n == 0:
        return 0.0
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def agreement(labels: dict[str, dict[str, bool]]) -> list[dict[str, Any]]:
    """Pairwise raw agreement and kappa over the items both judges graded."""
    out = []
    for j1, j2 in itertools.combinations(labels, 2):
        ids = sorted(set(labels[j1]) & set(labels[j2]))
        a = [labels[j1][i] for i in ids]
        b = [labels[j2][i] for i in ids]
        split = [i for i in ids if labels[j1][i] != labels[j2][i]]
        out.append(
            {
                "judges": [j1, j2],
                "n": len(ids),
                "agreement": round(
                    sum(x == y for x, y in zip(a, b, strict=True)) / max(1, len(ids)), 4
                ),
                "kappa": round(cohen_kappa(a, b), 4),
                "split_items": split,
            }
        )
    return out


def accuracy(labels: dict[str, bool], types: dict[str, str]) -> dict[str, Any]:
    by_type: dict[str, list[bool]] = {}
    for qid, ok in labels.items():
        by_type.setdefault(types.get(qid, ""), []).append(ok)
    return {
        "n": len(labels),
        "correct": sum(labels.values()),
        "accuracy": round(sum(labels.values()) / max(1, len(labels)), 4),
        "by_type": {
            t: {"n": len(v), "accuracy": round(sum(v) / len(v), 4)}
            for t, v in sorted(by_type.items())
        },
    }


async def _grade_all(
    items: list[dict[str, Any]], hyps: dict[str, str], judge: str, concurrency: int
) -> tuple[dict[str, bool], list[str]]:
    sem = asyncio.Semaphore(concurrency)
    labels: dict[str, bool] = {}
    errors: list[str] = []
    key = os.environ.get("OPENAI_API_KEY", "")

    async def one(client: httpx.AsyncClient, item: dict[str, Any]) -> None:
        qid = str(item["question_id"])
        async with sem:
            label, reply = await _llm_judge(
                client,
                str(item["question"]),
                str(item["answer"]),
                hyps[qid],
                judge,
                key,
                str(item["question_type"]),
                "_abs" in qid,
            )
        if label is None:
            errors.append(f"{qid}: {reply}")
        else:
            labels[qid] = label == "CORRECT"

    async with httpx.AsyncClient() as client:
        await asyncio.gather(*(one(client, it) for it in items))
    return labels, errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--hyp", required=True, help="<out>.hyp.jsonl from bench/longmemeval.py")
    ap.add_argument("--dataset", required=True, help="longmemeval_s_cleaned.json")
    ap.add_argument(
        "--judge", action="append", required=True, help="judge model; repeat for several"
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="grade only the first N hypotheses (0 = all)"
    )
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default="", help="write the full report as JSON here")
    args = ap.parse_args(argv)

    hyps: dict[str, str] = {}
    for line in Path(args.hyp).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            hyps[str(row["question_id"])] = str(row.get("hypothesis") or "")
    ids = list(hyps)[: args.limit or None]
    data = {
        str(it["question_id"]): it
        for it in json.loads(Path(args.dataset).read_text(encoding="utf-8"))
    }
    items = [data[i] for i in ids if i in data]
    types = {str(it["question_id"]): str(it["question_type"]) for it in items}

    labels: dict[str, dict[str, bool]] = {}
    report: dict[str, Any] = {"hyp": args.hyp, "n_items": len(items), "judges": {}}

    async def grade_every_judge() -> None:
        # One event loop for every judge: Loom's LLM clients share one HTTP pool.
        for judge in args.judge:
            got, errors = await _grade_all(items, hyps, judge, args.concurrency)
            labels[judge] = got
            report["judges"][judge] = {**accuracy(got, types), "errors": errors}
            acc = report["judges"][judge]
            print(
                f"{judge:28s} {acc['correct']}/{acc['n']} = {100 * acc['accuracy']:.1f}%"
                f"  errors={len(errors)}"
            )

    asyncio.run(grade_every_judge())
    report["agreement"] = agreement(labels)
    for a in report["agreement"]:
        print(
            f"{a['judges'][0]} vs {a['judges'][1]}: agreement {100 * a['agreement']:.1f}% "
            f"kappa {a['kappa']:.3f} over {a['n']} (split {len(a['split_items'])})"
        )
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
