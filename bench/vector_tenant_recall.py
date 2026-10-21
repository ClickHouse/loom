"""Do skip indexes change a SMALL tenant's nearest neighbours?

loom.memory_embeddings is one table holding many org/namespace tenants, and the
identity tuple is a PREWHERE filter. The bench compares a tenant-filtered nearest-
neighbour read (no FINAL) with skip indexes on against the same read with
`use_skip_indexes = 0`. A disagreement means some skip index changed which rows a
tenant read returns. With no vector index on the table they agree by construction, and
docs/architecture/clickhouse.md#why-there-is-no-vector-index measures why a
`vector_similarity` index would not even be searched by such a read. This bench keeps
the agreement a standing assertion against a live deployment.

    python bench/vector_tenant_recall.py --tenants 3 --probes 6 --min-agreement 0.95

Reads only. Needs the server's ClickHouse settings (.env in cwd).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_BASE = (
    "SELECT memory_key FROM loom.memory_embeddings "
    "PREWHERE org = {o:String} AND namespace = {n:String} "
    "WHERE tombstoned_at IS NULL AND length(embedding) = {d:UInt32} "
    "ORDER BY cosineDistance(embedding, {q:Array(Float32)}) LIMIT {k:UInt32}"
)


async def _tenants(ch: Any, dim: int, *, smallest: int, min_rows: int) -> list[dict[str, Any]]:
    return await ch.query_all(
        """
        SELECT org, namespace, count() AS c FROM loom.memory_embeddings
        WHERE tombstoned_at IS NULL AND length(embedding) = {d:UInt32}
        GROUP BY org, namespace HAVING c >= {m:UInt32}
        ORDER BY c ASC LIMIT {n:UInt32}
        """,
        {"d": dim, "m": min_rows, "n": smallest},
    )


async def measure(ch: Any, org: str, ns: str, *, dim: int, probes: int, k: int) -> tuple[int, int]:
    """(agreeing keys, brute-force keys) over `probes` random in-tenant queries.
    Keys are compared as sets: ReplacingMergeTree parts hold several versions
    of one key, and a duplicate must not count as a disagreement."""
    rows = await ch.query_all(
        "SELECT embedding FROM loom.memory_embeddings WHERE org = {o:String} AND "
        "namespace = {n:String} AND tombstoned_at IS NULL AND length(embedding) = {d:UInt32} "
        "ORDER BY rand() LIMIT {p:UInt32}",
        {"o": org, "n": ns, "d": dim, "p": probes},
    )
    agree = total = 0
    for r in rows:
        params = {"o": org, "n": ns, "d": dim, "q": list(r["embedding"]), "k": k}
        idx = {x["memory_key"] for x in await ch.query_all(_BASE, params)}
        brute_rows = await ch.query_all(_BASE + " SETTINGS use_skip_indexes = 0", params)
        brute = {x["memory_key"] for x in brute_rows}
        agree += len(idx & brute)
        total += len(brute)
    return agree, total


async def main_async(args: argparse.Namespace) -> int:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    total_rows = (
        await ch.query_all(
            "SELECT count() AS c FROM loom.memory_embeddings WHERE tombstoned_at IS NULL "
            "AND length(embedding) = {d:UInt32}",
            {"d": args.dim},
        )
    )[0]["c"]
    tenants = await _tenants(ch, args.dim, smallest=args.tenants, min_rows=args.min_rows)
    if not tenants:
        print("no tenants with enough rows; nothing to assert")
        return 0
    worst = 1.0
    print(
        f"table rows (dim {args.dim}): {total_rows:,} · top-{args.k} · {args.probes} probes/tenant"
    )
    for t in tenants:
        agree, total = await measure(
            ch,
            t["org"],
            t["namespace"],
            dim=args.dim,
            probes=args.probes,
            k=args.k,
        )
        rate = agree / total if total else 1.0
        worst = min(worst, rate)
        share = t["c"] / total_rows
        print(
            f"  {t['org']}/{t['namespace']:<32} rows={t['c']:>7} share={share:6.2%} "
            f"skip-indexes∩brute={agree}/{total} ({rate:.0%})"
        )
    if worst < args.min_agreement:
        print(
            f"FAIL: worst tenant agreement {worst:.0%} < {args.min_agreement:.0%}", file=sys.stderr
        )
        return 1
    print(f"ok: worst tenant agreement {worst:.0%}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tenants", type=int, default=3, help="smallest N tenants to probe")
    ap.add_argument("--min-rows", type=int, default=100, help="ignore tenants below this size")
    ap.add_argument("--probes", type=int, default=6)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--dim", type=int, default=3072)
    ap.add_argument("--min-agreement", type=float, default=0.95)
    args = ap.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
