#!/usr/bin/env python3
"""Concurrent load against one op: memory.set, memory.bulk_set, memory.get,
memory.search or knowledge.add.

Drives N workers for D seconds and reports p50 / p95 / p99 latency and RPS of
the successful calls, the error codes, and the deployment it measured (base
URL, build commit, schema version, the traffic class the server assigned).
It answers "at concurrency N, what is the steady-state p99 of one op?", not
how a real agent mix behaves.

memory.set, memory.bulk_set and knowledge.add WRITE rows into `--org` /
`--namespace` on the target and leave them there; the default namespace is a
fresh `bench-…` one, which the server classes as bench traffic (its own rate
buckets, lower ClickHouse priority). memory.search against a fresh namespace
searches nothing: point it at a namespace a memory.set or memory.bulk_set run
filled. memory.get first writes `--get-keys` keys into the namespace, then reads
them at random.

    scripts/ops/benchmark.py --op memory.set --concurrency 10 --duration 30
    scripts/ops/benchmark.py --op memory.search --concurrency 5 --duration 60 \\
        --namespace bench-1a2b3c4d
    scripts/ops/benchmark.py --op memory.set --json > result.json

Exit codes: 0 at least one call succeeded, 1 none did, 2 no token or the
server's health check failed (nothing was sent).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

OPS = ("memory.set", "memory.bulk_set", "memory.get", "memory.search", "knowledge.add")
WRITES = frozenset({"memory.set", "memory.bulk_set", "memory.get", "knowledge.add"})
BULK_ENTRIES = 20


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: the smallest value with at least `p` of the
    values at or below it. 0.0 for no values. Pure."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))]


def _rand_text(n: int = 64) -> str:
    return "".join(random.choices(string.ascii_lowercase + " ", k=n))


class Call:
    """One POST's outcome: HTTP status (0 = no response), latency, the traffic
    class the server answered with, and the exception name when there was none."""

    __slots__ = ("error", "latency_ms", "status", "traffic_class")

    def __init__(self, status: int, latency_ms: float, traffic_class: str = "", error: str = ""):
        self.status, self.latency_ms = status, latency_ms
        self.traffic_class, self.error = traffic_class, error


def _post(base_url: str, path: str, body: dict[str, Any], token: str, timeout: float) -> Call:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
            cls = r.headers.get("X-Loom-Traffic-Class", "")
            return Call(r.status, (time.perf_counter() - started) * 1000, cls)
    except urllib.error.HTTPError as e:
        cls = e.headers.get("X-Loom-Traffic-Class", "") if e.headers else ""
        return Call(e.code, (time.perf_counter() - started) * 1000, cls)
    except Exception as e:  # noqa: BLE001 - a failed call is a data point, not a crash
        return Call(0, (time.perf_counter() - started) * 1000, error=type(e).__name__)


def health(base_url: str, timeout: float = 10.0) -> dict[str, Any]:
    """The server's /v1/health body; raises when it does not answer 200."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/v1/health", timeout=timeout) as r:
        return dict(json.loads(r.read() or b"{}"))


def _get_key(i: int) -> str:
    return f"bench_get_{i}"


def _make_body(op: str, org: str, namespace: str, get_keys: int = 0) -> tuple[str, dict[str, Any]]:
    """(path, body) for one call of `op`."""
    if op == "memory.get":
        return "/v1/memory.get", {
            "org": org,
            "namespace": namespace,
            "agent": "bench",
            "memory_key": _get_key(random.randrange(max(1, get_keys))),
        }
    if op == "memory.bulk_set":
        return "/v1/memory.bulk_set", {
            "org": org,
            "namespace": namespace,
            "agent": "bench",
            "entries": [
                {
                    "memory_key": f"bench_{uuid.uuid4().hex[:12]}",
                    "memory_type": "semantic",
                    "value": _rand_text(128),
                }
                for _ in range(BULK_ENTRIES)
            ],
        }
    if op == "memory.set":
        return "/v1/memory.set", {
            "org": org,
            "namespace": namespace,
            "agent": "bench",
            "memory_key": f"bench_{uuid.uuid4().hex[:12]}",
            "memory_type": "semantic",
            "value": _rand_text(128),
        }
    if op == "memory.search":
        return "/v1/memory.search", {
            "org": org,
            "namespace": namespace,
            "agent": "bench",
            "query": _rand_text(48),
            "top_k": 5,
        }
    if op == "knowledge.add":
        return "/v1/knowledge.add", {
            "org": org,
            "namespace": namespace,
            "content": _rand_text(256),
            "source_uri": f"bench://{uuid.uuid4().hex}",
            "tags": ["bench"],
        }
    raise SystemExit(f"unsupported --op {op!r}; try {' / '.join(OPS)}")


def summarize(calls: list[Call], wall_s: float) -> dict[str, Any]:
    """Latency percentiles over the successful calls, RPS over the wall clock,
    and the failures by status (or exception name). Pure."""
    ok = [c.latency_ms for c in calls if c.status == 200]
    bad = [c for c in calls if c.status != 200]
    return {
        "duration_s": round(wall_s, 2),
        "rps": round(len(ok) / wall_s, 2) if ok and wall_s > 0 else 0.0,
        "p50_ms": round(percentile(ok, 0.50), 1),
        "p95_ms": round(percentile(ok, 0.95), 1),
        "p99_ms": round(percentile(ok, 0.99), 1),
        "max_ms": round(max(ok, default=0.0), 1),
        "mean_ms": round(statistics.fmean(ok), 1) if ok else 0.0,
        "ok": len(ok),
        "errors": len(bad),
        "error_codes": dict(sorted(Counter(str(c.error or c.status) for c in bad).items())),
        "traffic_class": dict(sorted(Counter(c.traffic_class or "-" for c in calls).items())),
    }


async def run(
    base_url: str,
    op: str,
    concurrency: int,
    duration: float,
    org: str,
    namespace: str,
    token: str,
    call_timeout_s: float = 30.0,
    get_keys: int = 0,
) -> dict[str, Any]:
    """`concurrency` workers POSTing `op` until `duration` has passed. Each
    worker has its own thread, so the default executor's size never caps the
    concurrency."""
    calls: list[Call] = []
    loop = asyncio.get_running_loop()
    end_time = time.monotonic() + duration

    async def worker(pool: ThreadPoolExecutor) -> None:
        while time.monotonic() < end_time:
            path, body = _make_body(op, org, namespace, get_keys)
            calls.append(
                await loop.run_in_executor(pool, _post, base_url, path, body, token, call_timeout_s)
            )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        await asyncio.gather(*(worker(pool) for _ in range(concurrency)))
    return {"op": op, "concurrency": concurrency, **summarize(calls, time.monotonic() - started)}


def seed(base_url: str, org: str, namespace: str, token: str, keys: int) -> None:
    """Write the keys memory.get reads; refuses to measure reads of missing rows."""
    for i in range(keys):
        call = _post(
            base_url,
            "/v1/memory.set",
            {
                "org": org,
                "namespace": namespace,
                "agent": "bench",
                "memory_key": _get_key(i),
                "memory_type": "semantic",
                "value": _rand_text(128),
            },
            token,
            30.0,
        )
        if call.status != 200:
            raise SystemExit(f"seeding {_get_key(i)} answered {call.status or call.error}")


def environment(args: argparse.Namespace, server: dict[str, Any]) -> dict[str, Any]:
    """What a result needs beside it to be compared with another run."""
    return {
        "base_url": args.base_url,
        "org": args.org,
        "namespace": args.namespace,
        "build_commit": server.get("build_commit", ""),
        "schema_version": server.get("schema_version", ""),
        "fake_embedding": server.get("fake_embedding"),
        "client_python": platform.python_version(),
        "client_host_cpus": os.cpu_count(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _token(arg: str | None) -> str:
    if arg:
        return arg
    tp = Path.home() / ".loom" / "token"
    return tp.read_text().strip() if tp.is_file() else ""


def _print_text(result: dict[str, Any]) -> None:
    ec = f" ({result['error_codes']})" if result["errors"] else ""
    env = result["env"]
    print()
    print(f"  op:          {result['op']}")
    print(f"  target:      {env['base_url']} {env['org']}/{env['namespace']}")
    print(f"  build:       {env['build_commit']} schema {env['schema_version']}")
    print(f"  class:       {result['traffic_class']}")
    print(f"  concurrency: {result['concurrency']}")
    print(f"  duration:    {result['duration_s']}s")
    print(f"  total ok:    {result['ok']}")
    print(f"  total err:   {result['errors']}{ec}")
    print(f"  rps:         {result['rps']}")
    print(
        f"  latency (ms): p50={result['p50_ms']}, "
        f"p95={result['p95_ms']}, p99={result['p99_ms']}, "
        f"max={result['max_ms']}, mean={result['mean_ms']}"
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument(
        "--token",
        default=os.environ.get("LOOM_API_KEY"),
        help="JWT; default $LOOM_API_KEY (or ~/.loom/token).",
    )
    p.add_argument("--op", default="memory.set", choices=OPS)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--duration", type=float, default=10.0, help="Wall-clock seconds to drive load.")
    p.add_argument("--org", default="bench")
    p.add_argument(
        "--namespace", default=f"bench-{uuid.uuid4().hex[:8]}", help="Fresh per run by default."
    )
    p.add_argument("--get-keys", type=int, default=50, help="Keys memory.get seeds and then reads.")
    p.add_argument(
        "--json", action="store_true", help="Emit one-line JSON to stdout (cron-friendly)."
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = _token(args.token)
    if not token:
        print(
            "Need a JWT — pass --token, set $LOOM_API_KEY, or write to ~/.loom/token",
            file=sys.stderr,
        )
        return 2
    try:
        server = health(args.base_url)
    except Exception as e:  # noqa: BLE001 - any failure to answer means no load is sent
        print(f"# {args.base_url}/v1/health failed: {e}; nothing sent", file=sys.stderr)
        return 2
    kind = "WRITES rows into" if args.op in WRITES else "reads"
    print(
        f"# benchmarking {args.op} at concurrency={args.concurrency} for {args.duration}s "
        f"against {args.base_url} (build {server.get('build_commit', '?')}); "
        f"{kind} {args.org}/{args.namespace}",
        file=sys.stderr,
    )
    env = environment(args, server)
    if args.op == "memory.get":
        seed(args.base_url, args.org, args.namespace, token, args.get_keys)
    result = asyncio.run(
        run(
            args.base_url,
            args.op,
            args.concurrency,
            args.duration,
            args.org,
            args.namespace,
            token,
            get_keys=args.get_keys,
        )
    )
    result["env"] = env
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_text(result)
    return 0 if result["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
