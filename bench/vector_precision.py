"""Does a narrower vector change what search returns? Measured, per namespace.

The store's bytes are vectors: a `memory_embeddings` row is about 17.6 KB
(two 3,072-dimension Float32 vectors) and a `memory_chunks` row about 11 KB,
against a few hundred bytes of memory text, so loom's byte lever is
precision. ClickHouse stores `Array(BFloat16)` and the HNSW index already
quantizes, so the question is empirical: for this namespace's real
vectors, how often does the bf16 top-k differ from the f32 top-k?

For `--queries` sampled stored vectors (leave-one-out), the bench takes the
f32 top-k by cosine and the top-k with BOTH sides cast to BFloat16 and back,
and reports overlap@k, top-1 agreement, and the rank the f32 top-1 lands at
under bf16 — plus the table's bytes per row from `system.parts`. Read-only;
no index involved (a brute-force scan on both arms, so the comparison is the
precision alone).

    python bench/vector_precision.py --org <org> --namespace <namespace> \
        --queries 40 --k 10
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
from pathlib import Path
from typing import Any

CAST_BF16 = "arrayMap(x -> toFloat32(x), CAST({col}, 'Array(BFloat16)'))"


def overlap_at_k(a: list[str], b: list[str], k: int) -> float:
    """|top-k(a) ∩ top-k(b)| / k."""
    if k <= 0:
        return 0.0
    return len(set(a[:k]) & set(b[:k])) / float(k)


def rank_of(key: str, ranked: list[str]) -> int | None:
    """1-based rank of `key` in `ranked`, None when absent."""
    try:
        return ranked.index(key) + 1
    except ValueError:
        return None


def summarize(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    """Aggregate per-query rows into the numbers the decision needs."""
    if not rows:
        return {"queries": 0}
    overlaps = [float(r["overlap"]) for r in rows]
    top1_same = sum(1 for r in rows if r["top1_same"])
    ranks = [r["f32_top1_rank_under_bf16"] for r in rows if r["f32_top1_rank_under_bf16"]]
    return {
        "queries": len(rows),
        "k": k,
        "overlap_at_k_mean": round(statistics.mean(overlaps), 4),
        "overlap_at_k_min": round(min(overlaps), 4),
        "queries_with_identical_topk": sum(1 for o in overlaps if o >= 1.0),
        "top1_agreement": round(top1_same / len(rows), 4),
        "f32_top1_rank_under_bf16_max": max(ranks) if ranks else None,
        "f32_top1_missing_under_bf16": sum(
            1 for r in rows if r["f32_top1_rank_under_bf16"] is None
        ),
    }


def _client(env_file: str | None) -> Any:
    env = dict(os.environ)
    if env_file and not Path(env_file).exists():
        raise SystemExit(f"--env-file {env_file} does not exist")
    if env_file:
        for line in Path(env_file).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                kk, v = line.split("=", 1)
                env.setdefault(kk.strip(), v.strip().strip("'").strip('"'))
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env["LOOM_CH_HOST"],
        port=int(env.get("LOOM_CH_PORT", "8443")),
        username=env["LOOM_CH_USER"],
        password=env["LOOM_CH_PASSWORD"],
        database=env.get("LOOM_CH_DATABASE", "loom"),
        secure=True,
        settings={"readonly": 1, "max_execution_time": 300},
    )


def topk_sql(*, cast_query: bool, cast_rows: bool, k: int) -> str:
    """Brute-force cosine top-k over the namespace's live vectors, self excluded.
    `cast_*` wrap the query vector / the stored vector in a BFloat16 round trip."""
    q = "{q:Array(Float32)}"
    if cast_query:
        q = CAST_BF16.format(col=q)
    col = "embedding"
    if cast_rows:
        col = CAST_BF16.format(col=col)
    return f"""
        SELECT memory_key
        FROM loom.memory_embeddings FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND tombstoned_at IS NULL AND embedding_dim > 0
          AND embedding_model = {{model:String}}
          AND memory_key != {{self:String}}
        ORDER BY cosineDistance({col}, {q}) ASC
        LIMIT {int(k)}
    """


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--org", required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument(
        "--env-file", default="", help="dotenv with LOOM_CH_*; default: the environment only"
    )
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    ch = _client(args.env_file)
    base = {"org": args.org, "ns": args.namespace}
    sample = ch.query(
        """
        SELECT memory_key, embedding, embedding_model
        FROM loom.memory_embeddings FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND tombstoned_at IS NULL AND embedding_dim > 0
        ORDER BY cityHash64(memory_key, {seed:UInt32}) LIMIT {n:UInt32}
        """,
        parameters={**base, "seed": args.seed, "n": args.queries},
    ).result_rows
    rows: list[dict[str, Any]] = []
    for key, vec, model in sample:
        p = {**base, "q": list(vec), "self": key, "model": model}
        f32 = [
            r[0]
            for r in ch.query(
                topk_sql(cast_query=False, cast_rows=False, k=args.k), parameters=p
            ).result_rows
        ]
        bf16 = [
            r[0]
            for r in ch.query(
                topk_sql(cast_query=True, cast_rows=True, k=args.k), parameters=p
            ).result_rows
        ]
        rows.append(
            {
                "query_key": key,
                "overlap": overlap_at_k(f32, bf16, args.k),
                "top1_same": bool(f32 and bf16 and f32[0] == bf16[0]),
                "f32_top1_rank_under_bf16": rank_of(f32[0], bf16) if f32 else None,
            }
        )
    parts = ch.query(
        """
        SELECT table, sum(rows) AS r, sum(bytes_on_disk) AS b FROM system.parts
        WHERE database = 'loom' AND active
          AND table IN ('memory_embeddings', 'memory_chunks', 'memory')
        GROUP BY table
        """
    ).result_rows
    bytes_per_row = {t: round(b / max(r, 1)) for t, r, b in parts}
    out = {**summarize(rows, args.k), "namespace": args.namespace, "bytes_per_row": bytes_per_row}
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": out, "rows": rows}, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
