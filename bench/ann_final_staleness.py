"""Does an HNSW index return stale versions under ReplacingMergeTree FINAL?

Builds a versioned table shaped like loom.memory_embeddings (3 versions per key, the
latest 20% tombstoned, parts left unmerged), then asks the same nearest-neighbour
question four ways and scores each against the exact answer (skip indexes off):

  final      SELECT ... FROM t FINAL ORDER BY cosineDistance LIMIT k     (loom's read)
  plain      same without FINAL
  prefilter  vector_search_filter_strategy = 'prefilter'                  (index unused)
  reread     index for candidate KEYS, then FINAL re-read of those keys
             with use_skip_indexes = 0                                    (stale rows dropped)

Run against any ClickHouse (default: a local one on :8125):

    python bench/ann_final_staleness.py --url http://localhost:8125 --user loom

Measured on CH 26.9 with the defaults (30k keys x 3 versions, D=128, 18 parts):
    final      recall@10 0.38, 6.2 stale rows, ~9 ms
    prefilter  recall@10 1.00, 0 stale rows,  ~20 ms
    reread x6  recall@10 0.98, 0 stale rows,  ~39 ms
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8125")
    ap.add_argument("--user", default="loom")
    ap.add_argument(
        "--password",
        default=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        help="default LOOM_CH_PASSWORD, else the dev stack's password",
    )
    ap.add_argument("--keys", type=int, default=30000)
    ap.add_argument("--dim", type=int, default=128)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--queries", type=int, default=5)
    ap.add_argument("--keep", action="store_true", help="leave the table behind")
    ap.add_argument(
        "--parts",
        type=int,
        default=0,
        help="after loading, leave the table in about this many parts (0 = as inserted, merges "
        "off; 1 = OPTIMIZE FINAL into one part) and print, per arm, the granules the HNSW "
        "index let the read skip (EXPLAIN indexes=1) — does part count matter",
    )
    ap.add_argument(
        "--quantization",
        default="",
        help="comma list of HNSW quantizations to rebuild the index with after the base arms, "
        "e.g. f32,bf16,i8,b1; each is scored with the final and reread arms "
        "and its index build time and size are printed",
    )
    args = ap.parse_args()

    def q(sql: str, settings: dict | None = None) -> tuple[str, float]:
        # Credentials in the auth headers, never the URL. Not read-only: the
        # bench creates, fills and drops its own table.
        params = {"allow_experimental_vector_similarity_index": "1"}
        if settings:
            params.update({k: str(v) for k, v in settings.items()})
        url = args.url.rstrip("/") + "/?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url,
            data=sql.encode(),
            headers={"X-ClickHouse-User": args.user, "X-ClickHouse-Key": args.password},
        )
        t0 = time.perf_counter()
        try:
            out = urllib.request.urlopen(req, timeout=900).read().decode()
        except urllib.error.HTTPError as e:  # surface ClickHouse's message, not "500"
            raise SystemExit(f"{sql[:120]}...\n{e.read().decode(errors='replace')[:800]}") from e
        return out, time.perf_counter() - t0

    rng = random.Random(2)

    def vec() -> list[float]:
        v = [rng.gauss(0, 1) for _ in range(args.dim)]
        s = sum(x * x for x in v) ** 0.5
        return [round(x / s, 5) for x in v]

    t = "loom.bench_ann_final_staleness"
    q(f"DROP TABLE IF EXISTS {t}")
    try:
        q(f"""CREATE TABLE {t} (org String, ns String, memory_key String, version UInt64,
            tombstoned_at Nullable(DateTime), embedding Array(Float32),
            INDEX emb_hnsw embedding TYPE vector_similarity('hnsw', 'cosineDistance', {args.dim})
                GRANULARITY 100000000)
            ENGINE = ReplacingMergeTree(version) ORDER BY (org, ns, memory_key)
            SETTINGS max_bytes_to_merge_at_max_space_in_pool = 1""")
        # Parts are kept unmerged by the table setting (no merge may exceed one
        # byte) rather than SYSTEM STOP MERGES, which would also block the index
        # mutations the quantization arms need.
        latest = [vec() for _ in range(args.keys)]
        for ver in (1, 2, 3):
            rows = []
            for i in range(args.keys):
                e = latest[i] if ver == 3 else vec()
                tomb = "2026-01-01 00:00:00" if (ver == 3 and i % 5 == 0) else None
                rows.append(
                    json.dumps(
                        {
                            "org": "o",
                            "ns": "n",
                            "memory_key": f"k{i}",
                            "version": ver,
                            "tombstoned_at": tomb,
                            "embedding": e,
                        }
                    )
                )
            for c in range(0, len(rows), 5000):
                q(f"INSERT INTO {t} FORMAT JSONEachRow\n" + "\n".join(rows[c : c + 5000]))
        q(f"ALTER TABLE {t} MATERIALIZE INDEX emb_hnsw SETTINGS mutations_sync=2")
        if args.parts == 1:
            q(
                f"ALTER TABLE {t} MODIFY SETTING "
                "max_bytes_to_merge_at_max_space_in_pool = 161061273600"
            )
            q(f"OPTIMIZE TABLE {t} FINAL")
            q(f"ALTER TABLE {t} MODIFY SETTING max_bytes_to_merge_at_max_space_in_pool = 1")
        parts = q(
            f"SELECT count() FROM system.parts WHERE database || '.' || table = '{t}' AND active"
        )[0].strip()
        print(f"table {t}: {args.keys} keys x 3 versions, {parts} active parts")

        def granules(sql: str) -> str:
            """'<kept>/<total>' granules after the HNSW skip index, from EXPLAIN."""
            import re as _re

            plan, _ = q("EXPLAIN indexes = 1 " + sql)
            m = _re.findall(r"Name: emb_hnsw\n.*?Granules: (\d+)/(\d+)", plan, _re.S)
            return "/".join(m[0]) if m else "n/a"

        def score(label: str, run) -> None:
            recalls, stales, times = [], [], []
            for _ in range(args.queries):
                target = latest[rng.randrange(args.keys)]
                qv = json.dumps(target)
                exact, _ = q(
                    f"SELECT memory_key FROM {t} FINAL WHERE org='o' AND ns='n' "
                    f"AND tombstoned_at IS NULL ORDER BY cosineDistance(embedding, {qv}) ASC "
                    f"LIMIT {args.k} SETTINGS use_skip_indexes=0"
                )
                truth = set(exact.split())
                out, dt = run(qv)
                lines = [line.split("\t") for line in out.strip().splitlines()]
                keys = {line[0] for line in lines}
                recalls.append(len(keys & truth) / args.k)
                stales.append(sum(1 for line in lines if line[1] != "3"))
                times.append(dt)
            print(
                f"  {label:12s} recall@{args.k}={statistics.mean(recalls):.2f} "
                f"stale_rows={statistics.mean(stales):.1f} "
                f"p50={1000 * statistics.median(times):.1f} ms"
            )

        base = (
            f"SELECT memory_key, version FROM {t}{{fin}} WHERE org='o' AND ns='n' "
            f"AND tombstoned_at IS NULL ORDER BY cosineDistance(embedding, {{qv}}) ASC "
            f"LIMIT {args.k}"
        )
        score("final", lambda qv: q(base.format(fin=" FINAL", qv=qv)))
        score("plain", lambda qv: q(base.format(fin="", qv=qv)))
        probe_vec = json.dumps(latest[0])
        print(
            f"  hnsw granules kept: final {granules(base.format(fin=' FINAL', qv=probe_vec))}, "
            f"plain {granules(base.format(fin='', qv=probe_vec))}"
        )
        score(
            "prefilter",
            lambda qv: q(
                base.format(fin=" FINAL", qv=qv), {"vector_search_filter_strategy": "prefilter"}
            ),
        )

        def reread(mult: int):
            def run(qv: str):
                t0 = time.perf_counter()
                cand, _ = q(
                    f"SELECT DISTINCT memory_key FROM {t} WHERE org='o' AND ns='n' "
                    f"ORDER BY cosineDistance(embedding, {qv}) ASC LIMIT {args.k * mult}"
                )
                klist = ",".join("'" + k + "'" for k in cand.split())
                out, _ = q(
                    f"SELECT memory_key, version FROM {t} FINAL WHERE org='o' AND ns='n' "
                    f"AND memory_key IN ({klist}) AND tombstoned_at IS NULL "
                    f"ORDER BY cosineDistance(embedding, {qv}) ASC LIMIT {args.k}",
                    {"use_skip_indexes": 0},
                )
                return out, time.perf_counter() - t0

            return run

        for mult in (3, 6):
            score(f"reread x{mult}", reread(mult))

        # The same question per index quantization. Data stays; only the index is dropped,
        # re-added and materialized, so build time and index bytes are per-quantization and
        # recall is against the same truth.
        for quant in [x.strip() for x in args.quantization.split(",") if x.strip()]:
            q(f"ALTER TABLE {t} DROP INDEX emb_hnsw")
            # Three or six arguments: a quantization needs M and ef_construction
            # beside it (ClickHouse defaults 32 and 128).
            q(
                f"ALTER TABLE {t} ADD INDEX emb_hnsw embedding TYPE vector_similarity("
                f"'hnsw', 'cosineDistance', {args.dim}, '{quant}', 32, 128) GRANULARITY 100000000"
            )
            _, build_s = q(f"ALTER TABLE {t} MATERIALIZE INDEX emb_hnsw SETTINGS mutations_sync=2")
            idx_bytes = q(
                f"SELECT sum(data_uncompressed_bytes) FROM system.data_skipping_indices "
                f"WHERE database || '.' || table = '{t}' AND name = 'emb_hnsw'"
            )[0].strip()
            print(f"quantization={quant}: index build {build_s:.1f}s, index bytes {idx_bytes}")
            score(f"  {quant} final", lambda qv: q(base.format(fin=" FINAL", qv=qv)))
            for mult in (3, 6):
                score(f"  {quant} reread x{mult}", reread(mult))
    finally:
        # Dropped on failure too, so an aborted run leaves no table behind.
        if not args.keep:
            q(f"DROP TABLE IF EXISTS {t}")


if __name__ == "__main__":
    main()
