"""ClickGap replay bench — the PR-review bot's integration gate.

Replays the exact call sequence ClickGap makes per PR review —
code.context_pack at analysis start, then code.symbol / code.callers /
code.impact per changed symbol — against N historical PRs, and reports:

  pack hit-rate       PRs whose precomputed pack existed (vs orientation
                      fallback)
  context tokens/PR   what the bot would inject instead of cold exploration
  structural coverage changed symbols that resolved (definition found) and
                      produced non-empty callers/impact
  latency             p50/p95 per call

This measures INTEGRATION READINESS (does the plane serve ClickGap's shape
of traffic, at what token cost); finding-quality A/B (judged hit-rate vs
gold findings) runs inside ClickGap's own harness where its prompts and
judge live — the two numbers gate the integration together.

Dataset: JSONL, NOT in-repo (redaction discipline: PR titles/symbols of the
public repo are fine, but the file is operator-produced from real traffic):
  {"pr_number": 12345, "changed_symbols": ["DB::Foo::bar", ...]}
Produce it from loom's own PR history (loom.code_pr_history).

Gates: an errored PR refuses the run (errors are excluded from the ratios, so
a partial run would otherwise read as healthy), and so does a dataset with no
changed symbols when --min-symbol-coverage is set.

Run:
  python bench/clickgap_code_replay.py --data prs.jsonl \
      --base-url http://localhost:7777 \
      --min-pack-hit 0.8 --min-symbol-coverage 0.7 --max-mean-tokens 4000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PrReplay:
    pr_number: int
    pack_hit: bool = False
    pack_tokens: int = 0
    symbols_total: int = 0
    symbols_resolved: int = 0
    symbols_with_callers: int = 0
    overrides_total: int = 0  # callers answers that resolved through a base (dispatch_seeds)
    overrides_with_callers: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    op_latencies_ms: dict[str, list[float]] = field(default_factory=dict)
    error: str = ""


def load_prs(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        row = json.loads(line)
        if "pr_number" not in row:
            raise ValueError(f"row missing pr_number: {row}")
        rows.append(row)
    if not rows:
        raise ValueError(f"{path}: no PR rows")
    return rows


async def replay_pr(client, row: dict) -> PrReplay:
    r = PrReplay(pr_number=int(row["pr_number"]))
    t0 = time.monotonic()
    resp = await client.post(
        "/v1/code.context_pack",
        json={"pr_number": r.pr_number, "consumer": "clickgap-replay"},
    )
    r.latencies_ms.append((time.monotonic() - t0) * 1000)
    r.op_latencies_ms.setdefault("context_pack", []).append(r.latencies_ms[-1])
    resp.raise_for_status()
    data = resp.json()
    r.pack_hit = data.get("source") == "precomputed"
    r.pack_tokens = int(data.get("tokens_used") or 0)

    for sym in row.get("changed_symbols") or []:
        r.symbols_total += 1
        t0 = time.monotonic()
        s = await client.post(
            "/v1/code.symbol",
            json={"name": sym, "include_lessons": False, "consumer": "clickgap-replay"},
        )
        r.latencies_ms.append((time.monotonic() - t0) * 1000)
        r.op_latencies_ms.setdefault("symbol", []).append(r.latencies_ms[-1])
        s.raise_for_status()
        sdata = s.json()
        if sdata.get("definitions"):
            r.symbols_resolved += 1
            t0 = time.monotonic()
            c = await client.post(
                "/v1/code.callers", json={"name": sym, "consumer": "clickgap-replay"}
            )
            r.latencies_ms.append((time.monotonic() - t0) * 1000)
            r.op_latencies_ms.setdefault("callers", []).append(r.latencies_ms[-1])
            c.raise_for_status()
            cdata = c.json()
            if cdata.get("edges"):
                r.symbols_with_callers += 1
            if cdata.get("dispatch_seeds"):
                r.overrides_total += 1
                if cdata.get("edges"):
                    r.overrides_with_callers += 1
    return r


async def run(args: argparse.Namespace) -> int:
    import httpx

    prs = load_prs(Path(args.data))
    print(f"replaying {len(prs)} PRs against {args.base_url}")
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    results: list[PrReplay] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0, headers=headers) as client:
        sem = asyncio.Semaphore(args.concurrency)

        async def _one(row: dict) -> None:
            async with sem:
                try:
                    results.append(await replay_pr(client, row))
                except Exception as e:  # noqa: BLE001 — errors excluded from ratios
                    results.append(PrReplay(pr_number=int(row["pr_number"]), error=str(e)[:200]))

        await asyncio.gather(*(_one(row) for row in prs))

    ok = [r for r in results if not r.error]
    errors = len(results) - len(ok)
    if not ok:
        print("every replay errored — is the server up and the index built?", file=sys.stderr)
        return 1

    pack_hit = sum(r.pack_hit for r in ok) / len(ok)
    mean_tokens = statistics.mean(r.pack_tokens for r in ok)
    sym_total = sum(r.symbols_total for r in ok)
    coverage = sum(r.symbols_resolved for r in ok) / sym_total if sym_total else 0.0
    caller_rate = sum(r.symbols_with_callers for r in ok) / sym_total if sym_total else 0.0
    lats = sorted(x for r in ok for x in r.latencies_ms)
    p50 = lats[len(lats) // 2] if lats else 0
    p95 = lats[int(0.95 * (len(lats) - 1))] if lats else 0

    print("\n=== clickgap_code_replay ===")
    print(f"PRs: {len(ok)}  errors(excluded): {errors}")
    for r in [r for r in results if r.error][:3]:
        print(f"  PR {r.pr_number}: {r.error}", file=sys.stderr)
    print(f"pack hit-rate:       {pack_hit:.1%}")
    print(f"pack tokens (mean):  {mean_tokens:.0f}")
    print(f"symbol coverage:     {coverage:.1%} of {sym_total} changed symbols resolved")
    print(f"callers coverage:    {caller_rate:.1%}")
    print(f"latency p50/p95:     {p50:.0f} / {p95:.0f} ms")
    per_op: dict[str, list[float]] = {}
    for r in ok:
        for op, ls in r.op_latencies_ms.items():
            per_op.setdefault(op, []).extend(ls)
    for op, ls in sorted(per_op.items()):
        ls = sorted(ls)
        p50o = ls[len(ls) // 2]
        p95o = ls[min(len(ls) - 1, int(len(ls) * 0.95))]
        print(f"  {op:<13} n={len(ls):>4}  p50 {p50o:>6.0f} ms  p95 {p95o:>6.0f} ms")
    ov_total = sum(r.overrides_total for r in ok)
    if ov_total:
        ov_hit = sum(r.overrides_with_callers for r in ok)
        print(
            f"override callers:    {ov_hit}/{ov_total} overrides resolved through a base "
            f"had callers ({ov_hit / ov_total:.1%})"
        )

    problems = []
    if errors:
        problems.append(f"{errors} PR replay(s) errored — metrics incomplete, gate refused")
    if args.min_symbol_coverage is not None and not sym_total:
        problems.append("no changed symbols in the dataset — symbol coverage not measured")
    if args.min_pack_hit is not None and pack_hit < args.min_pack_hit:
        problems.append(f"pack hit-rate {pack_hit:.2f} < {args.min_pack_hit}")
    if args.min_symbol_coverage is not None and sym_total and coverage < args.min_symbol_coverage:
        problems.append(f"symbol coverage {coverage:.2f} < {args.min_symbol_coverage}")
    if args.max_mean_tokens is not None and mean_tokens > args.max_mean_tokens:
        problems.append(f"mean pack tokens {mean_tokens:.0f} > {args.max_mean_tokens}")
    for p in problems:
        print(f"GATE FAIL: {p}", file=sys.stderr)
    if not problems:
        print("all gates passed")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", required=True, help="JSONL of {pr_number, changed_symbols}")
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--min-pack-hit", type=float, default=None)
    ap.add_argument("--min-symbol-coverage", type=float, default=None)
    ap.add_argument("--max-mean-tokens", type=float, default=None)
    args = ap.parse_args(argv)
    if not args.base_url:
        print("--base-url (or LOOM_BASE_URL) required", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
