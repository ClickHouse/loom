"""Is a vector index doing anything?

Read-only probe, over TLS, of a table shaped like `loom.memory_embeddings` (its
`status`, `embedding_dim` and `tombstoned_at` columns) that carries a `vector_similarity`
index (`--table`; no Loom table has one, so on `loom.memory_embeddings` it reports that
and exits): part-count
shape, HNSW index bytes, and, for a few namespaces and the whole org, how many granules
the index lets the read skip (EXPLAIN indexes=1), the latency of the production-shaped
query (FINAL, bare distance ORDER BY) against exact search, and recall of the indexed
read against exact. A sample reading: 83 parts (61 under 1,000 rows), 2.74 GiB of HNSW
for 596k rows, ≤17% of granules pruned org-wide, 0% inside a namespace, latency
identical to exact at every size, FINAL doubling the read (526 vs 271 ms org-wide).
docs/architecture/clickhouse.md#why-there-is-no-vector-index measures why: a
namespace-filtered read never searches the graph, even on one merged part.

    python bench/vector_index_health.py --env .env --namespaces a,b --org dev
"""

from __future__ import annotations

import argparse
import re
import statistics
import time
from pathlib import Path
from typing import Any

PRED = "e.embedding_dim > 0 AND e.status IN ('active','conflicted') AND e.tombstoned_at IS NULL"


def _client(env_path: str) -> Any:
    import clickhouse_connect
    from dotenv import dotenv_values

    if not Path(env_path).exists():
        raise SystemExit(f"--env {env_path} does not exist")
    e = dotenv_values(env_path)
    return clickhouse_connect.get_client(
        host=e["LOOM_CH_HOST"],
        port=int(e.get("LOOM_CH_PORT") or 8443),
        username=e["LOOM_CH_USER"],
        password=e["LOOM_CH_PASSWORD"],
        database=e.get("LOOM_CH_DATABASE") or "loom",
        secure=True,
    )


def dedup(rows: list[Any], k: int = 10) -> list[str]:
    out: list[str] = []
    for row in rows:
        if row[0] not in out:
            out.append(row[0])
    return out[:k]


def shape(c: Any, table: str) -> dict[str, Any]:
    db, tbl = table.split(".", 1)
    parts = c.query(
        "SELECT count(), countIf(rows < 1000), countIf(rows < 10000), sum(rows), "
        "formatReadableSize(sum(bytes_on_disk)), "
        "formatReadableSize(sum(secondary_indices_compressed_bytes)) "
        "FROM system.parts WHERE database = {db:String} AND table = {t:String} AND active",
        parameters={"db": db, "t": tbl},
    ).result_rows[0]
    idx = c.query(
        "SELECT name, type_full, formatReadableSize(data_compressed_bytes) "
        "FROM system.data_skipping_indices "
        "WHERE database = {db:String} AND table = {t:String} AND type = 'vector_similarity'",
        parameters={"db": db, "t": tbl},
    ).result_rows
    return {
        "parts": parts[0],
        "parts_under_1k_rows": parts[1],
        "parts_under_10k_rows": parts[2],
        "rows": parts[3],
        "table_bytes": parts[4],
        "index_bytes": parts[5],
        "vector_indexes": [(n, t, b) for n, t, b in idx],
    }


def shortfall(live_rows: int, limit: int, returned: int) -> int:
    """Rows a vector read returned short of what the scope holds. ClickHouse
    #117286: after a lightweight DELETE a read that opens
    the vector index can return fewer than LIMIT rows and miss true
    neighbours. `min(live, limit) - returned`, floored at 0; anything above 0
    on an indexed scope is the defect showing."""
    return max(0, min(int(live_rows), int(limit)) - int(returned))


def lightweight_delete_probe(
    c: Any, table: str, where: str, params: dict[str, Any], vecs: list[list[float]], limit: int = 60
) -> dict[str, Any]:
    """Count live rows in the scope, then run the indexed and the exact read
    with the same LIMIT; report the shortfall of each. Read-only."""
    live = c.query(
        f"SELECT count() FROM {table} AS e FINAL WHERE {where} AND {PRED}", parameters=params
    ).result_rows[0][0]
    idx_settings = (
        "SETTINGS hnsw_candidate_list_size_for_search = 256, "
        "vector_search_index_fetch_multiplier = 10.0"
    )
    worst_idx = worst_exact = 0
    for v in vecs:
        for settings, bucket in ((idx_settings, "idx"), ("SETTINGS use_skip_indexes = 0", "exact")):
            rows = c.query(
                f"SELECT e.memory_key FROM {table} AS e FINAL WHERE {where} AND {PRED} "
                f"ORDER BY cosineDistance(e.embedding, {{v:Array(Float32)}}) ASC "
                f"LIMIT {limit} {settings}",
                parameters={**params, "v": v},
            ).result_rows
            short = shortfall(live, limit, len(dedup(rows, limit)))
            if bucket == "idx":
                worst_idx = max(worst_idx, short)
            else:
                worst_exact = max(worst_exact, short)
    return {
        "live_rows": int(live),
        "limit": limit,
        "shortfall_indexed": worst_idx,
        "shortfall_exact": worst_exact,
    }


def probe(
    c: Any, table: str, where: str, params: dict[str, Any], vecs: list[list[float]], index_name: str
) -> dict[str, Any]:
    idx_settings = (
        "SETTINGS hnsw_candidate_list_size_for_search = 256, "
        "vector_search_index_fetch_multiplier = 10.0"
    )

    def q(fin: str, settings: str, v: list[float]) -> tuple[list[str], float]:
        sql = (
            f"SELECT e.memory_key FROM {table} AS e{fin} WHERE {where} AND {PRED} "
            f"ORDER BY cosineDistance(e.embedding, {{v:Array(Float32)}}) ASC LIMIT 60 {settings}"
        )
        t0 = time.perf_counter()
        rows = c.query(sql, parameters={**params, "v": v}).result_rows
        return dedup(rows), (time.perf_counter() - t0) * 1000

    lat: dict[str, list[float]] = {"final_exact": [], "final_index": [], "plain_index": []}
    rec: list[float] = []
    for v in vecs:
        ex, a = q(" FINAL", "SETTINGS use_skip_indexes = 0", v)
        ix, b = q(" FINAL", idx_settings, v)
        _, d = q("", idx_settings, v)
        lat["final_exact"].append(a)
        lat["final_index"].append(b)
        lat["plain_index"].append(d)
        rec.append(len(set(ix) & set(ex)) / max(1, len(ex)))
    plan = "\n".join(
        r[0]
        for r in c.query(
            f"EXPLAIN indexes = 1 SELECT e.memory_key FROM {table} AS e FINAL "
            f"WHERE {where} AND {PRED} "
            f"ORDER BY cosineDistance(e.embedding, {{v:Array(Float32)}}) ASC "
            f"LIMIT 60 {idx_settings}",
            parameters={**params, "v": vecs[0]},
        ).result_rows
    )
    m = re.findall(rf"Name: {re.escape(index_name)}\n.*?Granules: (\d+)/(\d+)", plan, re.S)
    return {
        "granules_kept_by_index": "/".join(m[0]) if m else "index not in plan",
        "p50_ms": {k: round(statistics.median(v)) for k, v in lat.items()},
        "index_recall_vs_exact": round(statistics.mean(rec), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--env", required=True, help="dotenv with LOOM_CH_* of the deployment to read")
    ap.add_argument("--table", default="loom.memory_embeddings")
    ap.add_argument("--org", default="dev")
    ap.add_argument("--namespaces", default="", help="comma list; default = 3 largest in the org")
    ap.add_argument("--queries", type=int, default=3, help="probe vectors per scope (stored rows)")
    args = ap.parse_args()
    c = _client(args.env)
    sh = shape(c, args.table)
    print("shape:", sh)
    if not sh["vector_indexes"]:
        print("no vector_similarity index on the table")
        return 0
    index_name = sh["vector_indexes"][0][0]
    nss = [n for n in args.namespaces.split(",") if n] or [
        r[0]
        for r in c.query(
            f"SELECT namespace FROM {args.table} WHERE org = {{org:String}} AND embedding_dim > 0 "
            "GROUP BY namespace ORDER BY count() DESC LIMIT 3",
            parameters={"org": args.org},
        ).result_rows
    ]
    for ns in nss:
        vecs = [
            [float(x) for x in v]
            for (v,) in c.query(
                f"SELECT embedding FROM {args.table} FINAL WHERE org = {{org:String}} "
                f"AND namespace = {{ns:String}} "
                f"AND embedding_dim > 0 ORDER BY cityHash64(memory_key) LIMIT {int(args.queries)}",
                parameters={"org": args.org, "ns": ns},
            ).result_rows
        ]
        if not vecs:
            print(f"{ns}: no rows")
            continue
        n = c.query(
            f"SELECT uniq(memory_key) FROM {args.table} FINAL WHERE org = {{org:String}} "
            "AND namespace = {ns:String} AND embedding_dim > 0",
            parameters={"org": args.org, "ns": ns},
        ).result_rows[0][0]
        print(
            f"namespace {ns} ({n} keys):",
            probe(
                c,
                args.table,
                "e.org = {org:String} AND e.namespace = {ns:String}",
                {"org": args.org, "ns": ns},
                vecs,
                index_name,
            ),
        )
        print(
            "  lightweight-delete shortfall:",
            lightweight_delete_probe(
                c,
                args.table,
                "e.org = {org:String} AND e.namespace = {ns:String}",
                {"org": args.org, "ns": ns},
                vecs,
            ),
        )
    vecs = [
        [float(x) for x in v]
        for (v,) in c.query(
            f"SELECT embedding FROM {args.table} FINAL WHERE org = {{org:String}} "
            f"AND embedding_dim > 0 ORDER BY cityHash64(memory_key) LIMIT {int(args.queries)}",
            parameters={"org": args.org},
        ).result_rows
    ]
    print(
        f"org {args.org} (all namespaces):",
        probe(c, args.table, "e.org = {org:String}", {"org": args.org}, vecs, index_name),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
