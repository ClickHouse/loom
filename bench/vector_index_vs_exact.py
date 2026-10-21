"""Exact scan vs a `vector_similarity` (HNSW) index under Loom's tenant filter.

Builds four copies of a table shaped like `loom.memory_embeddings` (its sort key,
ReplacingMergeTree, `index_granularity = 256`) in the database you point it at: plain,
`Quantized`-coded, HNSW-indexed, and HNSW-indexed with one partition per named tenant.
Tenants of 50%, 10%, 1% and 0.1% of the rows share one synthetic vector space (500
clusters around a 16-dimensional latent space); the rest spread over 1000 namespaces.
Each query is a noisy copy of a row its filter admits, and recall@10 is against the exact
top 10 under the same filter. The read columns come from `system.query_log`: "index
used" is `ProfileEvents['USearchSearchCount'] > 0`, "cache misses" is
`VectorSimilarityIndexCacheMisses`, so they report what ran.

Filters: `all` (none), a tenant `(org, namespace)` (the sort-key prefix every Loom read
carries), and `10pct` / `1pct`, predicates outside the sort key that admit 10% and 1% of
all rows. The partitioned table is measured after the others, so its graphs do not
compete with the single-part graph for the index cache; the last row reads the
single-part graph again once they have.

Needs no provider and no Loom server, only a ClickHouse you started for this. It writes
the `vb_*` tables into `--database` (created if missing, left in place) and drops the
tables at the end, failed runs included, unless `--keep`. The run in
docs/architecture/clickhouse.md#why-there-is-no-vector-index (about 40 minutes and
12 GiB of disk):

    PYTHONPATH=$PWD .venv/bin/python bench/vector_index_vs_exact.py \
        --port 18433 --user loom --password loomdev --rows 1000000 --dim 1536 --build
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
import uuid

import clickhouse_connect

TENANTS = [("big", 0.50), ("mid", 0.10), ("small", 0.01), ("tiny", 0.001)]
TENANT_NAMES = [ns for ns, _ in TENANTS]
# Predicates outside the sort key, by the per-mille of rows they admit.
HASH_FILTERS = {"10pct": 100, "1pct": 10}
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
        named = ", ".join(f"'{ns}'" for ns in TENANT_NAMES)
        partition = f"if(namespace IN ({named}), toString(namespace), 'rest')"
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
    # Small blocks: the projection holds rows x dim x 16 floats per block.
    ch.command(
        f"INSERT INTO vb_plain {select}", settings={"max_block_size": 256, "max_threads": 16}
    )
    print(f"insert plain (generates the vectors): {time.monotonic() - t:.1f}s", flush=True)
    for kind in KINDS[1:]:
        t = time.monotonic()
        ch.command(
            f"INSERT INTO vb_{kind} SELECT * FROM vb_plain", settings={"enable_quantized_codec": 1}
        )
        print(f"insert {kind}: {time.monotonic() - t:.1f}s", flush=True)
    for kind in KINDS:
        t = time.monotonic()
        ch.command(f"OPTIMIZE TABLE vb_{kind} FINAL")
        print(f"merge {kind} into one part per partition: {time.monotonic() - t:.1f}s", flush=True)


def predicate(name: str) -> str:
    if name == "all":
        return ""
    if name in HASH_FILTERS:
        return f"WHERE modulo(cityHash64(memory_key), 1000) < {HASH_FILTERS[name]}"
    return (
        "PREWHERE org = 'o' AND namespace = {ns:String}"
        " WHERE tombstoned_at IS NULL AND embedding_dim > 0"
    )


def nearest(ch, table: str, name: str, q: list[float], *, final: bool, limit: int, settings: dict):
    sql = f"""
        SELECT memory_key FROM {table} {"FINAL" if final else ""} {predicate(name)}
        ORDER BY cosineDistance(embedding, {{q:Array(Float32)}}) ASC
        LIMIT {limit}
    """
    t = time.monotonic()
    rows = ch.query(sql, parameters={"ns": name, "q": q}, settings=settings).result_rows
    return [r[0] for r in rows], (time.monotonic() - t) * 1000


def logged(ch, tag: str) -> tuple[int, int, float, float]:
    """Index searches, index cache misses, mean rows read and mean MiB read of the
    queries tagged `tag`."""
    ch.command("SYSTEM FLUSH LOGS")
    r = ch.query(
        "SELECT sum(ProfileEvents['USearchSearchCount']),"
        " sum(ProfileEvents['VectorSimilarityIndexCacheMisses']),"
        " avg(read_rows), avg(read_bytes) / 1048576"
        " FROM system.query_log WHERE type = 'QueryFinish' AND log_comment = {t:String}",
        parameters={"t": tag},
    ).result_rows[0]
    return int(r[0]), int(r[1]), float(r[2]), float(r[3])


POST = {"vector_search_filter_strategy": "postfilter"}
# (label, table, FINAL, LIMIT, settings). recall@10 counts the exact top 10 found in the
# LIMIT rows a variant returns; the fact leg asks for at least 60 and reranks them.
VARIANTS = [
    ("exact FINAL", "vb_plain", True, 10, {}),
    (
        "codes-first x3 FINAL",
        "vb_codes",
        True,
        10,
        {"vector_search_use_quantized_codes": 1, "vector_search_index_fetch_multiplier": 3},
    ),
    (
        "codes-first x1 FINAL, LIMIT 60",
        "vb_codes",
        True,
        60,
        {"vector_search_use_quantized_codes": 1, "vector_search_index_fetch_multiplier": 1},
    ),
    ("hnsw FINAL", "vb_hnsw", True, 10, {}),
    ("hnsw x1", "vb_hnsw", False, 10, POST),
    ("hnsw x10", "vb_hnsw", False, 10, {**POST, "vector_search_index_fetch_multiplier": 10}),
    ("hnsw x100", "vb_hnsw", False, 10, {**POST, "vector_search_index_fetch_multiplier": 100}),
    (
        "hnsw x100 ef1024",
        "vb_hnsw",
        False,
        10,
        {
            **POST,
            "vector_search_index_fetch_multiplier": 100,
            "hnsw_candidate_list_size_for_search": 1024,
        },
    ),
]
PARTITIONED = [
    ("hnsw, partition per tenant, FINAL", "vb_hnsw_part", True, 10, {}),
    ("hnsw, partition per tenant", "vb_hnsw_part", False, 10, {}),
]
CACHE_CHECK = [("hnsw x1, after the partitioned graphs loaded", "vb_hnsw", False, 10, POST)]


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
    ap.add_argument("--keep", action="store_true", help="leave the tables behind")
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
    try:
        if args.build:
            build(ch, args.rows, args.dim)
        measure(ch, args)
    finally:
        # Also on a failed or interrupted run: the 1M-row tables take about 12 GiB.
        if not args.keep:
            for kind in KINDS:
                ch.command(f"DROP TABLE IF EXISTS vb_{kind} SYNC")


def measure(ch, args: argparse.Namespace) -> None:
    for kind in KINDS:
        r = ch.query(
            "SELECT count(), sum(rows), formatReadableSize(sum(data_compressed_bytes)),"
            " formatReadableSize(sum(secondary_indices_compressed_bytes)) FROM system.parts"
            " WHERE database = currentDatabase() AND table = {t:String} AND active",
            parameters={"t": f"vb_{kind}"},
        ).result_rows[0]
        print(f"vb_{kind}: parts={r[0]} rows={r[1]} table data={r[2]} index={r[3]}")
    # The cache-check row depends on these.
    cache = ch.query(
        "SELECT name, value FROM system.server_settings"
        " WHERE name LIKE 'vector_similarity_index_cache_%'"
    ).result_rows
    print("server: " + ", ".join(f"{n}={v}" for n, v in cache))
    # Tags carry a run id: system.query_log keeps every earlier run's rows.
    run = uuid.uuid4().hex[:8]
    rnd = random.Random(7)
    filters = ["all"] + TENANT_NAMES + list(HASH_FILTERS)
    queries: dict[str, list[list[float]]] = {}
    truth: dict[str, list[set[str]]] = {}
    admitted: dict[str, int] = {}
    for name in filters:
        cond = predicate(name)
        admitted[name] = ch.query(
            f"SELECT count() FROM vb_plain {cond}", parameters={"ns": name}
        ).result_rows[0][0]
        seeds = ch.query(
            f"SELECT embedding FROM vb_plain {cond}"
            " ORDER BY cityHash64(memory_key) LIMIT {n:UInt32}",
            parameters={"ns": name, "n": args.queries},
        ).result_rows
        queries[name] = [[x + rnd.gauss(0, 0.05) for x in s[0]] for s in seeds]
        truth[name] = [
            set(nearest(ch, "vb_plain", name, q, final=True, limit=10, settings={})[0])
            for q in queries[name]
        ]

    print(f"\nrows={args.rows} dim={args.dim} queries/filter={args.queries} run={run}")
    print(
        "| filter | rows admitted | variant | index used | cache misses | recall@10"
        " | p50 ms | p95 ms | rows read | MiB read |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    passes = [(VARIANTS, filters), (PARTITIONED, ["all"] + TENANT_NAMES), (CACHE_CHECK, ["all"])]
    for variants, names in passes:
        for name in names:
            for label, table, final, limit, st in variants:
                nearest(ch, table, name, queries[name][0], final=final, limit=limit, settings=st)
                tag = f"vb:{run}:{name}:{label}"
                recalls, times = [], []
                for q, tr in zip(queries[name], truth[name], strict=True):
                    got, ms = nearest(
                        ch,
                        table,
                        name,
                        q,
                        final=final,
                        limit=limit,
                        settings={**st, "log_comment": tag},
                    )
                    recalls.append(len(tr & set(got)) / max(1, len(tr)))
                    times.append(ms)
                times.sort()
                p95 = times[min(len(times) - 1, int(len(times) * 0.95))]
                searches, misses, read_rows, read_mib = logged(ch, tag)
                if name == "all" and label == "hnsw x1" and not searches:
                    raise SystemExit("an unfiltered read searched no index: the event name changed")
                print(
                    f"| {name} | {admitted[name]} | {label} | {'yes' if searches else 'no'}"
                    f" | {misses} | {statistics.mean(recalls):.3f}"
                    f" | {statistics.median(times):.0f} | {p95:.0f} | {read_rows:.0f}"
                    f" | {read_mib:.1f} |",
                    flush=True,
                )


if __name__ == "__main__":
    main()
