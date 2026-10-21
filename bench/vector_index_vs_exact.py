"""Exact scan vs a `vector_similarity` (HNSW) index under Loom's tenant filter.

Builds four copies of a table shaped like `loom.memory_embeddings` (its sort key,
ReplacingMergeTree, `index_granularity = 256`) in the database you point it at: plain,
`Quantized`-coded, HNSW-indexed, and HNSW-indexed with one partition per named tenant.
Tenants of 50%, 10%, 1% and 0.1% of the rows share one synthetic vector space (500
clusters around a 16-dimensional latent space); the rest spread over 1000 namespaces.
Each query is a noisy copy of a row its filter admits, and recall@10 is
against the exact top 10 under the same filter. "index used" reads
`ProfileEvents['USearchSearchCount']` from `system.query_log`, so it reports what ran.

Filters: `all` (none), a tenant `(org, namespace)` (the sort-key prefix every Loom read
carries), and `hash100` / `hash10`, predicates outside the sort key that admit 10% and 1%
of all rows.

Needs no provider and no Loom server; it writes tables `vb_*` and drops them on `--build`.
Use a ClickHouse you started for this:

    PYTHONPATH=$PWD .venv/bin/python bench/vector_index_vs_exact.py \
        --port 18433 --user loom --password loomdev --rows 100000 --dim 1536 --build

Results and what they decided: docs/architecture/clickhouse.md#why-there-is-no-vector-index.
"""

from __future__ import annotations

import argparse
import statistics
import time

import clickhouse_connect
import numpy as np

TENANTS = [("big", 0.50), ("mid", 0.10), ("small", 0.01), ("tiny", 0.001)]
KINDS = ("plain", "codes", "hnsw", "hnsw_part")
COLUMNS = """
    org LowCardinality(String), namespace LowCardinality(String), agent LowCardinality(String),
    user_id String, session_id String, scope Enum8('private' = 1, 'shared' = 2),
    embedding_model LowCardinality(String), memory_key String,
    embedding_dim UInt16, tombstoned_at Nullable(DateTime), version UInt64, created_at DateTime,
"""
KEY = "ORDER BY (org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key)"


def ddl(name: str, dim: int, kind: str) -> str:
    partition = "toYYYYMM(created_at)"
    if kind == "hnsw_part":
        # One partition per named tenant, so a tenant filter selects whole parts.
        partition = "if(namespace IN ('big', 'mid', 'small', 'tiny'), toString(namespace), 'rest')"
        kind = "hnsw"
    codec = f" CODEC(Quantized('rabitq', {dim}, 0))" if kind == "codes" else ""
    index = (
        ", INDEX embedding_hnsw_idx embedding TYPE vector_similarity("
        f"'hnsw', 'cosineDistance', {dim}, 'bf16', 16, 128) GRANULARITY 100000000"
        if kind == "hnsw"
        else ""
    )
    return (
        f"CREATE TABLE {name} ({COLUMNS} embedding Array(BFloat16){codec}{index}) "
        f"ENGINE = ReplacingMergeTree(version) PARTITION BY {partition} {KEY} "
        "SETTINGS index_granularity = 256, min_bytes_for_wide_part = 0"
    )


def namespace_expr() -> str:
    parts, lo = [], 0.0
    for ns, share in TENANTS:
        lo += share
        parts.append(f"u < {lo}, '{ns}'")
    return f"multiIf({', '.join(parts)}, concat('n', toString(number % 1000)))"


def build(ch, rows: int, dim: int) -> None:
    for kind in KINDS:
        ch.command(f"DROP TABLE IF EXISTS vb_{kind} SYNC")
        ch.command(ddl(f"vb_{kind}", dim, kind), settings={"enable_quantized_codec": 1})
    # Text embeddings have a low intrinsic dimension: each vector is a cluster centroid
    # (500 clusters) plus a point of a 16-dimensional latent space projected into `dim`
    # dimensions by a fixed matrix, plus a little isotropic noise. Centroid and matrix
    # entries are uniform in [-1, 1] from a hash.
    select = f"""
        SELECT 'o' AS org, {namespace_expr()} AS namespace, 'a' AS agent, '-' AS user_id,
               '-' AS session_id, 'private' AS scope, 'm' AS embedding_model,
               concat('k', toString(number)) AS memory_key, {dim} AS embedding_dim,
               NULL AS tombstoned_at, 1 AS version, toDateTime('2026-10-01 00:00:00') AS created_at,
               arrayMap(i -> toBFloat16(
                   0.6 * ((cityHash64(number % 500, i) % 2000) / 1000.0 - 1.0)
                   + arraySum(j -> z[j + 1] * ((cityHash64('w', j, i) % 2000) / 1000.0 - 1.0),
                              range(16)) / 4
                   + randNormal(0, 0.05)), range({dim})) AS embedding
        FROM (SELECT number, (cityHash64(number, 'tenant') % 1000000) / 1000000.0 AS u,
                     arrayMap(j -> randNormal(0, 1), range(16)) AS z
              FROM numbers({rows}))
    """
    t = time.monotonic()
    ch.command(
        f"INSERT INTO vb_plain {select}", settings={"max_block_size": 256, "max_threads": 16}
    )
    print(f"insert plain: {time.monotonic() - t:.1f}s", flush=True)
    for kind in KINDS[1:]:
        t = time.monotonic()
        ch.command(
            f"INSERT INTO vb_{kind} SELECT * FROM vb_plain", settings={"enable_quantized_codec": 1}
        )
        print(f"insert {kind}: {time.monotonic() - t:.1f}s", flush=True)
    for kind in KINDS:
        t = time.monotonic()
        ch.command(f"OPTIMIZE TABLE vb_{kind} FINAL")
        print(f"optimize {kind}: {time.monotonic() - t:.1f}s", flush=True)


def predicate(ns: str) -> str:
    if ns == "all":
        return ""
    if ns.startswith("hash"):
        return f"WHERE cityHash64(memory_key) % 1000 < {int(ns[4:])}"
    return (
        "PREWHERE org = 'o' AND namespace = {ns:String}"
        " WHERE tombstoned_at IS NULL AND embedding_dim > 0"
    )


def topk(ch, table: str, ns: str, q: list[float], *, final: bool, settings: dict, limit: int = 10):
    sql = f"""
        SELECT memory_key FROM {table} {"FINAL" if final else ""} {predicate(ns)}
        ORDER BY cosineDistance(embedding, {{q:Array(Float32)}}) ASC
        LIMIT {limit}
    """
    t = time.monotonic()
    rows = ch.query(sql, parameters={"ns": ns, "q": q}, settings=settings).result_rows
    return [r[0] for r in rows], (time.monotonic() - t) * 1000


def logged(ch, tag: str) -> tuple[int, float, float]:
    """Index searches, mean rows read and mean MiB read of the timed queries tagged `tag`."""
    ch.command("SYSTEM FLUSH LOGS")
    r = ch.query(
        "SELECT sum(ProfileEvents['USearchSearchCount']), avg(read_rows), avg(read_bytes) / 1048576"
        " FROM system.query_log WHERE type = 'QueryFinish' AND log_comment = {t:String}",
        parameters={"t": tag},
    ).result_rows[0]
    return int(r[0]), float(r[1]), float(r[2])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=8123, help="HTTP port")
    ap.add_argument("--user", default="default")
    ap.add_argument("--password", default="")
    ap.add_argument("--database", default="loom_vbench", help="created on --build")
    ap.add_argument("--rows", type=int, default=100_000)
    ap.add_argument("--dim", type=int, default=1536)
    ap.add_argument("--queries", type=int, default=30, help="queries per filter")
    ap.add_argument("--build", action="store_true", help="(re)create and fill the tables first")
    args = ap.parse_args()
    if args.build:
        admin = clickhouse_connect.get_client(
            host=args.host, port=args.port, username=args.user, password=args.password
        )
        admin.command(f"CREATE DATABASE IF NOT EXISTS {args.database}")
    ch = clickhouse_connect.get_client(
        host=args.host,
        port=args.port,
        username=args.user,
        password=args.password,
        database=args.database,
        send_receive_timeout=7200,
    )
    if args.build:
        build(ch, args.rows, args.dim)
    for kind in KINDS:
        r = ch.query(
            f"SELECT count(), sum(rows), formatReadableSize(sum(data_compressed_bytes)),"
            f" formatReadableSize(sum(secondary_indices_compressed_bytes)) FROM system.parts"
            f" WHERE database = currentDatabase() AND table = 'vb_{kind}' AND active"
        ).result_rows[0]
        print(f"vb_{kind}: parts={r[0]} rows={r[1]} data={r[2]} index={r[3]}")

    rng = np.random.default_rng(7)
    exact = {"vector_search_filter_strategy": "prefilter"}
    post = {"vector_search_filter_strategy": "postfilter"}
    variants = [
        ("exact plain FINAL", "vb_plain", True, exact),
        (
            "exact codes-first FINAL",
            "vb_codes",
            True,
            {"vector_search_use_quantized_codes": 1, "vector_search_index_fetch_multiplier": 3},
        ),
        ("hnsw FINAL", "vb_hnsw", True, {}),
        ("hnsw x1", "vb_hnsw", False, post),
        ("hnsw x10", "vb_hnsw", False, {**post, "vector_search_index_fetch_multiplier": 10}),
        ("hnsw x100", "vb_hnsw", False, {**post, "vector_search_index_fetch_multiplier": 100}),
        (
            "hnsw x100 ef1024",
            "vb_hnsw",
            False,
            {
                **post,
                "vector_search_index_fetch_multiplier": 100,
                "hnsw_candidate_list_size_for_search": 1024,
            },
        ),
        (
            "hnsw x10 ef1024 rescored",
            "vb_hnsw",
            False,
            {
                **post,
                "vector_search_index_fetch_multiplier": 10,
                "hnsw_candidate_list_size_for_search": 1024,
                "vector_search_with_rescoring": 1,
            },
        ),
        ("hnsw, partition per tenant, FINAL", "vb_hnsw_part", True, {}),
        ("hnsw, partition per tenant", "vb_hnsw_part", False, {}),
    ]
    print(f"\nrows={args.rows} dim={args.dim} queries/filter={args.queries}")
    print(
        "| filter | rows admitted | variant | index used | recall@10"
        " | p50 ms | p95 ms | rows read | MiB read |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for ns in ["all"] + [t[0] for t in TENANTS] + ["hash100", "hash10"]:
        if ns == "all" or ns.startswith("hash"):
            cond = predicate(ns)
            n_ns = ch.query(f"SELECT count() FROM vb_plain {cond}").result_rows[0][0]
            seeds = ch.query(
                f"SELECT embedding FROM vb_plain {cond} ORDER BY memory_key LIMIT {args.queries}"
            ).result_rows
        else:
            n_ns = ch.query(
                "SELECT count() FROM vb_plain WHERE namespace = {ns:String}", parameters={"ns": ns}
            ).result_rows[0][0]
            seeds = ch.query(
                "SELECT embedding FROM vb_plain WHERE namespace = {ns:String}"
                " ORDER BY cityHash64(memory_key) LIMIT {n:UInt32}",
                parameters={"ns": ns, "n": args.queries},
            ).result_rows
        queries = [
            (np.array(s[0], dtype=np.float32) + rng.normal(0, 0.05, args.dim))
            .astype(np.float32)
            .tolist()
            for s in seeds
        ]
        truth = [set(topk(ch, "vb_plain", ns, q, final=True, settings=exact)[0]) for q in queries]
        for label, table, final, st in variants:
            if ns.startswith("hash") and table == "vb_hnsw_part":
                continue
            topk(ch, table, ns, queries[0], final=final, settings=st)  # warm
            tag = f"vb:{args.rows}:{ns}:{label}"
            recalls, times = [], []
            for q, tr in zip(queries, truth, strict=True):
                got, ms = topk(ch, table, ns, q, final=final, settings={**st, "log_comment": tag})
                recalls.append(len(tr & set(got)) / max(1, len(tr)))
                times.append(ms)
            times.sort()
            p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
            searches, read_rows, read_mib = logged(ch, tag)
            print(
                f"| {ns} | {n_ns} | {label} | {'yes' if searches else 'no'}"
                f" | {statistics.mean(recalls):.3f} |"
                f" {statistics.median(times):.0f} | {p95:.0f} | {read_rows:.0f} | {read_mib:.1f} |",
                flush=True,
            )


if __name__ == "__main__":
    main()
