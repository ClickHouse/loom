#!/usr/bin/env python3
"""A/B one per-request search switch (key_match, ...) on one running server.

Each question is searched twice through /v1/memory.search — the switch
false, then true — with the same top_k, and the gold memory key's rank is
read from both answers. Reports recall@5 / recall@10 / MRR per arm, how many
questions the switch changed, how many rows it added (`retrieval_work.
<field>_added`), and lists the questions where the arms disagree.

Questions: the route_replay `sample` output (jsonl with query, org,
namespace, gold_key).

    python bench/search_switch_ab.py --questions route_questions.jsonl \\
        --base-url https://loom.example --token $TOK --top-k 10 \\
        --field key_match --out key_match_ab.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

import httpx


def rank_of(keys: list[str], gold: str) -> int:
    """1-based rank of gold in keys, 0 when absent."""
    for i, k in enumerate(keys, start=1):
        if k == gold:
            return i
    return 0


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure: per-arm recall@5, recall@10, MRR and the disagreement list from
    per-question rows {qid, rank_off, rank_on, added}."""
    n = len(rows)

    def arm(key: str) -> dict[str, float]:
        ranks = [int(r[key]) for r in rows]
        return {
            "recall@5": sum(1 for x in ranks if 0 < x <= 5) / n if n else 0.0,
            "recall@10": sum(1 for x in ranks if 0 < x <= 10) / n if n else 0.0,
            "mrr": sum(1.0 / x for x in ranks if x > 0) / n if n else 0.0,
        }

    changed = [r for r in rows if r["rank_off"] != r["rank_on"]]
    return {
        "n": n,
        "off": arm("rank_off"),
        "on": arm("rank_on"),
        "changed": len(changed),
        "improved": sum(1 for r in changed if (r["rank_on"] or 10**6) < (r["rank_off"] or 10**6)),
        "regressed": sum(1 for r in changed if (r["rank_on"] or 10**6) > (r["rank_off"] or 10**6)),
        "rows_added_total": sum(int(r.get("added") or 0) for r in rows),
        "questions_with_added_rows": sum(1 for r in rows if int(r.get("added") or 0) > 0),
        "disagreements": [
            {
                "qid": r["qid"],
                "rank_off": r["rank_off"],
                "rank_on": r["rank_on"],
                "added": r.get("added", 0),
            }
            for r in changed
        ],
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    questions = [json.loads(ln) for ln in open(args.questions, encoding="utf-8") if ln.strip()]
    if args.limit:
        questions = questions[: args.limit]
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        timeout=60.0,
        headers={"Authorization": f"Bearer {args.token}"},
    ) as c:
        sem = asyncio.Semaphore(int(args.concurrency))

        async def one(q: dict[str, Any]) -> None:
            body = {
                "org": q["org"],
                "namespace": q["namespace"],
                "agent": args.agent,
                "query": q["query"],
                "top_k": int(args.top_k),
            }
            async with sem:
                r_off = await c.post("/v1/memory.search", json={**body, args.field: False})
                r_on = await c.post("/v1/memory.search", json={**body, args.field: True})
            r_off.raise_for_status()
            r_on.raise_for_status()
            off, on = r_off.json(), r_on.json()
            keys_off = [h["memory_key"] for h in off.get("results", [])]
            keys_on = [h["memory_key"] for h in on.get("results", [])]
            work = on.get("retrieval_work") or {}
            rows.append(
                {
                    "qid": q.get("qid", ""),
                    "gold_key": q.get("gold_key", ""),
                    "rank_off": rank_of(keys_off, q.get("gold_key", "")),
                    "rank_on": rank_of(keys_on, q.get("gold_key", "")),
                    "added": int(work.get(f"{args.field}_added") or 0),
                    "sources_on": sorted({h.get("source", "fact") for h in on.get("results", [])}),
                }
            )

        await asyncio.gather(*(one(q) for q in questions))
    rows.sort(key=lambda r: r["qid"])
    return {"summary": summarise(rows), "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--questions", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--agent", default="*")
    ap.add_argument(
        "--field",
        default="key_match",
        help="the per-request boolean switch to A/B (key_match, ...)",
    )
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    result = asyncio.run(run(args))
    s = result["summary"]
    for arm in ("off", "on"):
        a = s[arm]
        print(
            f"{arm:>3}: r@5={a['recall@5']:.3f} r@10={a['recall@10']:.3f} "
            f"mrr={a['mrr']:.3f}  (n={s['n']})"
        )
    print(
        f"changed={s['changed']} improved={s['improved']} regressed={s['regressed']} "
        f"rows_added={s['rows_added_total']} on {s['questions_with_added_rows']} questions"
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
