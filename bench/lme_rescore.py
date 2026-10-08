"""Re-score an existing LongMemEval hypotheses file with the published-protocol lexical
columns (bench/answer_scoring.py) — no ingest, no reader, no judge; only the optional
simplification call.

`bench/longmemeval.py --answer-judge` writes `<out>.hyp.jsonl` (question_id,
hypothesis). This reads that file and the dataset, simplifies each answer with
`--simplify-model` (or the model-free heuristic when none is given) and prints
mean token F1 / BLEU-1 overall and per question type, in percent as published
lexical tables report them. Use it to put a lexical number on runs
that predate `--lexical-scores`, or to compare simplifiers on one set of
answers without paying for the reader again.

    OPENAI_API_KEY=... .venv/bin/python bench/lme_rescore.py \\
        --hyp bench/longmemeval.hyp.jsonl \\
        --dataset bench/datasets/longmemeval_s_cleaned.json \\
        --simplify-model gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from bench.answer_scoring import bleu1, rouge_l, token_f1


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Mean F1 / BLEU-1 in percent, overall and per question_type, plus the
    unsimplified pair so the effect of the simplification step is visible."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_type.setdefault(str(r.get("question_type", "")), []).append(r)

    def mean(rs: list[dict[str, Any]], k: str) -> float:
        return round(100.0 * sum(float(r.get(k, 0.0)) for r in rs) / max(1, len(rs)), 2)

    return {
        "n": len(rows),
        "f1": mean(rows, "f1"),
        "bleu1": mean(rows, "bleu1"),
        "rouge_l_f1": mean(rows, "rouge_l_f1"),
        "rouge_l_recall": mean(rows, "rouge_l_recall"),
        "f1_raw": mean(rows, "f1_raw"),
        "bleu1_raw": mean(rows, "bleu1_raw"),
        "by_type": {
            t: {"n": len(rs), "f1": mean(rs, "f1"), "bleu1": mean(rs, "bleu1")}
            for t, rs in sorted(by_type.items())
        },
    }


def score_row(item: dict[str, Any], hypothesis: str, simplified: str, by: str) -> dict[str, Any]:
    gold = str(item["answer"])
    return {
        "question_id": item["question_id"],
        "question_type": item.get("question_type", ""),
        "gold": gold,
        "hypothesis": hypothesis,
        "simplified": simplified,
        "simplified_by": by,
        "f1_raw": token_f1(hypothesis, gold),
        "bleu1_raw": bleu1(hypothesis, gold),
        "f1": token_f1(simplified, gold),
        "bleu1": bleu1(simplified, gold),
        "rouge_l_f1": rouge_l(simplified, gold)[0],
        "rouge_l_recall": rouge_l(simplified, gold)[1],
    }


async def rescore(
    hyp_path: Path, dataset_path: Path, simplify_model: str, concurrency: int
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    import httpx

    from bench.longmemeval import _simplify_hypothesis

    items = {str(i["question_id"]): i for i in json.loads(dataset_path.read_text())}
    hyps = [json.loads(ln) for ln in hyp_path.read_text().splitlines() if ln.strip()]
    api_key = os.environ.get("OPENAI_API_KEY", "")
    usage: dict[str, int] = {"tin": 0, "tout": 0, "calls": 0}
    rows: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient() as client:

        async def one(h: dict[str, Any]) -> None:
            item = items.get(str(h.get("question_id")))
            if item is None:
                return
            hyp = str(h.get("hypothesis") or "")
            async with sem:
                simplified, by = await _simplify_hypothesis(
                    client, str(item["question"]), hyp, simplify_model, api_key, usage_sink=usage
                )
            rows.append(score_row(item, hyp, simplified, by))

        await asyncio.gather(*[one(h) for h in hyps])
    rows.sort(key=lambda r: str(r["question_id"]))
    return summarize(rows), rows, usage


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hyp", required=True, help="hypotheses JSONL written by longmemeval.py")
    ap.add_argument("--dataset", required=True, help="LongMemEval dataset JSON")
    ap.add_argument(
        "--simplify-model",
        default="",
        help="OpenAI model for the simplification step; empty = the model-free heuristic",
    )
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="", help="write rows + summary as JSON")
    args = ap.parse_args(argv)
    if args.simplify_model and not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is required for --simplify-model", file=sys.stderr)
        return 2
    summary, rows, usage = asyncio.run(
        rescore(Path(args.hyp), Path(args.dataset), args.simplify_model, args.concurrency)
    )
    how = (
        f"simplified by {args.simplify_model}"
        if args.simplify_model
        else "heuristic simplification"
    )
    print(f"LongMemEval lexical re-score ({how}; n={summary['n']}; published tokenisation)")
    print(
        f"  F1 {summary['f1']:.2f}   BLEU-1 {summary['bleu1']:.2f}   "
        f"ROUGE-L F1 {summary['rouge_l_f1']:.2f}   ROUGE-L R {summary['rouge_l_recall']:.2f}"
    )
    print(f"  unsimplified: F1 {summary['f1_raw']:.2f}   BLEU-1 {summary['bleu1_raw']:.2f}")
    for t, s in summary["by_type"].items():
        print(f"  {t:<28} n={s['n']:<3} F1 {s['f1']:>6.2f}  BLEU-1 {s['bleu1']:>6.2f}")
    if usage.get("tin"):
        print(f"  simplifier tokens: in {usage['tin']} out {usage['tout']}")
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
