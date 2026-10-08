"""memory.search latency and context size on a finished LongMemEval run.

Replays each question of a `bench/longmemeval.py --keep-namespaces` CSV (its
`question` and `namespace` columns) as one `memory.search`, one call at a time,
with the server's default read path, and reports the client-side wall time
p50 / p95 and the context a reader would get at `--top-k` (excerpt characters / 4).
Sequential on purpose: concurrency measures the queue, not the read.

    python -m bench.lme_search_latency --csv out/lme_full.csv --base-url http://127.0.0.1:7961 \\
        --token-file out/bench.token --top-k 15 --out out/search_latency.json
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from bench._http import apost


def percentile(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(round(q * (len(v) - 1)))))] if v else 0.0


async def run(args: argparse.Namespace) -> dict[str, Any]:
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    rows = [r for r in csv.DictReader(open(args.csv, encoding="utf-8")) if r.get("namespace")]
    seen: set[str] = set()
    queries = []
    for r in rows:
        if r["question_id"] not in seen:
            seen.add(r["question_id"])
            queries.append(r)
    walls: list[float] = []
    tokens: list[int] = []
    errors = 0
    async with httpx.AsyncClient(timeout=60.0) as client:
        for r in queries[: args.limit or None]:
            body = {
                "org": args.org,
                "namespace": r["namespace"],
                "agent": args.agent,
                "query": r["question"],
                "top_k": args.top_k,
            }
            t0 = time.perf_counter()
            try:
                resp = await apost(client, f"{args.base_url}/v1/memory.search", body, token=token)
            except httpx.HTTPError:
                errors += 1
                continue
            walls.append((time.perf_counter() - t0) * 1000.0)
            hits = resp.json().get("results") or []
            chars = sum(len(str(h.get("content_excerpt") or h.get("value") or "")) for h in hits)
            tokens.append(chars // 4)
    return {
        "csv": args.csv,
        "n": len(walls),
        "errors": errors,
        "top_k": args.top_k,
        "search_ms_p50": round(statistics.median(walls)) if walls else 0,
        "search_ms_p95": round(percentile(walls, 0.95)),
        "context_tokens_mean": round(statistics.mean(tokens)) if tokens else 0,
        "context_tokens_p50": round(statistics.median(tokens)) if tokens else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--csv", required=True)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token-file", required=True)
    ap.add_argument("--org", default="dev")
    ap.add_argument("--agent", default="lme-bench")
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    summary = asyncio.run(run(args))
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
