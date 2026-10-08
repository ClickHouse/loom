"""Loom microbenchmark harness — measures wire-level p50/p99 for
`memory.set` and `memory.search` against a running Loom instance.

The harness is corpus-shape-aware but deliberately *does not* run an
extraction LLM call on the write side — `memory.set` is a pure wire
write at ~0 tokens. Numbers from this bench should be compared
against other systems' *write*-path numbers, not against their
extract-then-write pipelines (which spend thousands of tokens per
call running an LLM to decide what to remember).

The corpus is synthetic conversational data shaped like LoCoMo turns
(role + content + timestamp). We don't ship LoCoMo itself (it's a
3rd-party dataset); instead we generate N turns with realistic
content lengths and let the harness measure how Loom scales.

Run:
    python bench/locomo.py --base-url http://localhost:7777 \\
        --corpus-sizes 1000,10000 \\
        --concurrency 8 --search-queries 100 \\
        --out bench/results.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import statistics
import string
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

try:
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

ORG = "dev"


@dataclass
class TimingSample:
    op: str
    corpus_size: int
    latency_ms: float
    ok: bool


def _random_sentence(min_words: int = 6, max_words: int = 30) -> str:
    n = random.randint(min_words, max_words)
    return " ".join(
        "".join(random.choices(string.ascii_lowercase, k=random.randint(3, 9))) for _ in range(n)
    )


def _synthetic_memory(i: int, memory_type: str) -> dict[str, object]:
    """A LoCoMo-shaped memory: realistic content length, a couple of tags.
    Pass memory_type="episodic" to skip the embed call entirely — useful
    for measuring Loom's pure wire-write p50 without an embedder in the
    loop. Searchable types (semantic / procedural / preference) embed
    inline."""
    return {
        "memory_key": f"bench_mem_{i:08d}",
        "value": _random_sentence(20, 80),
        "memory_type": memory_type,
        "tags": random.sample(
            ["ops", "runbook", "preference", "fact", "note", "project"],
            k=random.randint(1, 3),
        ),
    }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


async def _post(
    client: httpx.AsyncClient, path: str, body: dict[str, object]
) -> tuple[float, bool]:
    t0 = time.perf_counter()
    try:
        r = await client.post(path, json=body, timeout=10.0)
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    return (time.perf_counter() - t0) * 1000.0, ok


def _bookkeeping_post(
    client: httpx.AsyncClient,
) -> Callable[[str, dict[str, object]], Awaitable[object]]:
    """The raising `post(path, body)` bench_namespaces takes for register and
    erase; the timed `_post` above swallows errors on purpose."""

    async def post(path: str, body: dict[str, object]) -> object:
        r = await client.post(path, json=body, timeout=120.0)
        r.raise_for_status()
        return r.json()

    return post


async def _mint(client: httpx.AsyncClient, namespace: str, keep: bool) -> dict[str, str]:
    """Register a throwaway namespace as bench and remember it for the erasure."""
    remember_bench_namespace(ORG, namespace)
    await register_bench_namespace(
        _bookkeeping_post(client),
        org=ORG,
        namespace=namespace,
        description="LoCoMo-shaped wire latency corpus",
        keep=keep,
    )
    return {"org": ORG, "namespace": namespace, "agent": "bench", "user_id": "-"}


async def _populate(
    client: httpx.AsyncClient,
    identity: dict[str, str],
    n: int,
    concurrency: int,
    memory_type: str,
    op_label: str,
) -> list[TimingSample]:
    """Write n memories. Reports per-call timing under op_label."""
    sem = asyncio.Semaphore(concurrency)
    samples: list[TimingSample] = []

    async def one(i: int) -> None:
        async with sem:
            body = {**identity, **_synthetic_memory(i, memory_type)}
            lat, ok = await _post(client, "/v1/memory.set", body)
            samples.append(TimingSample(op_label, n, lat, ok))

    await asyncio.gather(*(one(i) for i in range(n)))
    return samples


async def _search_round(
    client: httpx.AsyncClient,
    identity: dict[str, str],
    n: int,
    queries: int,
    concurrency: int,
) -> list[TimingSample]:
    sem = asyncio.Semaphore(concurrency)
    samples: list[TimingSample] = []

    async def one() -> None:
        async with sem:
            body = {
                **identity,
                "query": _random_sentence(3, 8),
                "top_k": 5,
                "search_mode": "hybrid",
                "alpha": 0.5,
            }
            lat, ok = await _post(client, "/v1/memory.search", body)
            samples.append(TimingSample("memory.search", n, lat, ok))

    await asyncio.gather(*(one() for _ in range(queries)))
    return samples


async def _run_one_size(
    client: httpx.AsyncClient,
    base_namespace: str,
    corpus_size: int,
    search_queries: int,
    concurrency: int,
    keep: bool = False,
) -> list[TimingSample]:
    """Three measurements per corpus size:
    1) memory.set (semantic) — full flow, includes embedder
    2) memory.set (episodic) — wire-only, embedder skipped
    3) memory.search — hybrid mode against the populated corpus
    """
    namespace = f"{base_namespace}-{corpus_size}-{uuid.uuid4().hex[:6]}"
    identity = await _mint(client, namespace, keep)

    print(f"  [{corpus_size:>7}] populating semantic (embedder in-loop)...", flush=True)
    semantic = await _populate(
        client,
        identity,
        corpus_size,
        concurrency,
        memory_type="semantic",
        op_label="memory.set (+embed)",
    )

    print(f"  [{corpus_size:>7}] populating episodic (wire-only)...", flush=True)
    # Separate namespace so episodic writes don't pollute the search corpus.
    episodic_id = await _mint(client, namespace + "-wire", keep)
    episodic = await _populate(
        client,
        episodic_id,
        corpus_size,
        concurrency,
        memory_type="episodic",
        op_label="memory.set (wire-only)",
    )

    print(f"  [{corpus_size:>7}] searching...", flush=True)
    search_samples = await _search_round(client, identity, corpus_size, search_queries, concurrency)

    return semantic + episodic + search_samples


def _summarise(samples: list[TimingSample]) -> list[dict[str, object]]:
    by_op_size: dict[tuple[str, int], list[float]] = {}
    err_count: dict[tuple[str, int], int] = {}
    for s in samples:
        key = (s.op, s.corpus_size)
        by_op_size.setdefault(key, []).append(s.latency_ms)
        if not s.ok:
            err_count[key] = err_count.get(key, 0) + 1
    rows = []
    for (op, size), lats in sorted(by_op_size.items()):
        rows.append(
            {
                "op": op,
                "corpus_size": size,
                "n": len(lats),
                "p50_ms": round(_percentile(lats, 0.5), 2),
                "p99_ms": round(_percentile(lats, 0.99), 2),
                "mean_ms": round(statistics.mean(lats), 2) if lats else 0.0,
                "errors": err_count.get((op, size), 0),
            }
        )
    return rows


def _write_csv(out_path: Path, rows: list[dict[str, object]]) -> None:
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument(
        "--corpus-sizes",
        default="1000,10000",
        help="comma-separated list (e.g. 1000,10000,100000)",
    )
    parser.add_argument("--search-queries", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--out", default="bench/results.csv")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    parser.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase this run's bench-* namespaces at the end",
    )
    args = parser.parse_args()

    random.seed(args.seed)
    sizes = [int(x) for x in args.corpus_sizes.split(",") if x.strip()]
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}

    print(
        f"loom-bench against {args.base_url}, sizes={sizes}, "
        f"concurrency={args.concurrency}, search_queries={args.search_queries}"
    )

    async with httpx.AsyncClient(base_url=args.base_url, headers=headers, timeout=30.0) as client:
        # Health gate so the harness fails fast on a missing/dead server.
        r = await client.get("/v1/health")
        if r.status_code != 200:
            print(f"server unhealthy at {args.base_url}: {r.status_code}", file=sys.stderr)
            return 2

        all_samples: list[TimingSample] = []
        # The minted namespaces are erased on exit, after a failure or Ctrl-C too.
        async with erase_on_exit(_bookkeeping_post(client), org=ORG, keep=args.keep_namespaces):
            for size in sizes:
                all_samples.extend(
                    await _run_one_size(
                        client,
                        "bench",
                        size,
                        args.search_queries,
                        args.concurrency,
                        keep=args.keep_namespaces,
                    )
                )

    rows = _summarise(all_samples)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_write_csv, out_path, rows)

    print()
    print(
        f"{'op':<16} {'corpus':>8} {'n':>6} {'p50_ms':>8} {'p99_ms':>8} "
        f"{'mean_ms':>8} {'errors':>7}"
    )
    for r in rows:
        print(
            f"{r['op']:<16} {r['corpus_size']:>8} {r['n']:>6} "
            f"{r['p50_ms']:>8} {r['p99_ms']:>8} {r['mean_ms']:>8} "
            f"{r['errors']:>7}"
        )
    print(f"\nwrote {out_path}")

    # Fail loud when every op errored. Latency numbers from a 100%-error
    # run are misleading — they measure how fast the server rejected the
    # call, not how fast it served it. The most common cause is a token
    # without permissions on the dynamic per-run namespace; mint a
    # broader-scoped token (loom mint-token --roles admin) and retry.
    total_n = sum(int(r["n"]) for r in rows)
    total_errors = sum(int(r["errors"]) for r in rows)
    if total_n > 0 and total_errors == total_n:
        print(
            "\nERROR: every op errored. The latency numbers above are "
            "noise (rejection latency, not service latency). Most "
            "likely the token doesn't authorize the per-run namespaces "
            "this bench creates. Mint with `loom mint-token --roles "
            "admin --namespaces '*'` and re-run.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
