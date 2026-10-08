"""LongMemEval-S with the whole chat history in the reader's prompt: the no-memory baseline.

Each question's haystack (every session, in date order) goes to the reader in the official
answer template, in place of Loom's retrieved memories, and the official judge grades the
answer. Paired with a `bench/longmemeval.py` CSV of the same questions, it reports both
accuracies and the context each read, so the memory's saving is stated beside what it cost
in accuracy.

    OPENAI_API_KEY=... python -m bench.lme_full_context \\
        --dataset bench/datasets/longmemeval_s_cleaned.json \\
        --pair bench/results/longmemeval_s.csv --pair-arm reader_gpt5_k200 \\
        --reader-model gpt-5 --limit 100 --out out/lme_full_context.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
from typing import Any

import httpx

from bench.judges import achat, is_claude
from bench.longmemeval import _OFFICIAL_ANSWER_TEMPLATE, _llm_judge


def approx_tokens(text: str) -> int:
    return len(text) // 4


def history_text(item: dict[str, Any]) -> str:
    """Every haystack session, oldest first, one dated block each: the official
    full-history rendering."""
    sessions = sorted(
        zip(item["haystack_dates"], item["haystack_sessions"], strict=False),
        key=lambda pair: pair[0],
    )
    blocks = []
    for i, (date, turns) in enumerate(sessions):
        body = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        blocks.append(f"### Session {i + 1}:\nSession Date: {date}\nSession Content:\n{body}\n")
    return "\n".join(blocks)


def pair_rows(path: str, arm: str) -> dict[str, dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["question_id"]: r for r in csv.DictReader(fh) if r.get("arm", arm) == arm}


async def one(
    client: httpx.AsyncClient,
    item: dict[str, Any],
    reader: str,
    judge: str,
    key: str,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    history = history_text(item)
    prompt = _OFFICIAL_ANSWER_TEMPLATE.format(
        history=history, date=item.get("question_date") or "unknown", question=item["question"]
    )
    async with sem:
        try:
            answer, usage = await achat(
                client,
                reader,
                prompt,
                max_tokens=2048,
                timeout_s=300.0,
                api_key="" if is_claude(reader) else key,
            )
        except httpx.HTTPError as e:
            answer, usage = f"(reader error: {e})", {}
        label, _ = await _llm_judge(
            client,
            item["question"],
            str(item["answer"]),
            answer,
            judge,
            key,
            item["question_type"],
            "_abs" in str(item["question_id"]),
        )
    return {
        "question_id": item["question_id"],
        "question_type": item["question_type"],
        "context_tokens_est": approx_tokens(history),
        "reader_prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "reader_completion_tokens": int(usage.get("completion_tokens") or 0),
        "judge_label": label or "",
        "hypothesis": answer[:4000],
    }


async def main_async(args: argparse.Namespace) -> int:
    with open(args.dataset, encoding="utf-8") as fh:
        items = json.load(fh)
    paired = pair_rows(args.pair, args.pair_arm) if args.pair else {}
    if paired:
        items = [it for it in items if it["question_id"] in paired]
    random.Random(args.seed).shuffle(items)
    items = items[: args.limit]
    key = os.environ.get("OPENAI_API_KEY", "")
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        rows = await asyncio.gather(
            *(one(client, it, args.reader_model, args.judge_model, key, sem) for it in items)
        )
    n = len(rows)
    full_ok = sum(r["judge_label"] == "CORRECT" for r in rows)
    summary: dict[str, Any] = {
        "n": n,
        "reader_model": args.reader_model,
        "judge_model": args.judge_model,
        "full_history_accuracy": round(full_ok / n, 4) if n else 0.0,
        "full_history_context_tokens_mean": round(sum(r["context_tokens_est"] for r in rows) / n),
        "full_history_reader_prompt_tokens_mean": round(
            sum(r["reader_prompt_tokens"] for r in rows) / n
        ),
    }
    if paired:
        mem = [paired[r["question_id"]] for r in rows]
        mem_ok = sum(m["judge_label"] == "CORRECT" for m in mem)
        # The paired run's prompt tokens on the answering calls (its reader and judge,
        # so a slight overcount against the memory side).
        ctx = [int(m.get("qa_tokens_in") or 0) for m in mem]
        summary.update(
            {
                "paired_arm": args.pair_arm,
                "memory_accuracy": round(mem_ok / n, 4),
                "memory_prompt_tokens_mean": round(sum(ctx) / n),
                "both_correct": sum(
                    r["judge_label"] == "CORRECT" and m["judge_label"] == "CORRECT"
                    for r, m in zip(rows, mem, strict=True)
                ),
                "only_memory_correct": sum(
                    r["judge_label"] != "CORRECT" and m["judge_label"] == "CORRECT"
                    for r, m in zip(rows, mem, strict=True)
                ),
                "only_full_history_correct": sum(
                    r["judge_label"] == "CORRECT" and m["judge_label"] != "CORRECT"
                    for r, m in zip(rows, mem, strict=True)
                ),
            }
        )
    print(json.dumps(summary, indent=1))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"summary": summary, "rows": rows}, fh, indent=1)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--pair", default="", help="a bench/longmemeval.py CSV of the same questions")
    ap.add_argument("--pair-arm", default="reader_gpt5_k200")
    ap.add_argument("--reader-model", default="gpt-5")
    ap.add_argument("--judge-model", default="gpt-4o-2024-08-06")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--seed", type=int, default=13)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
