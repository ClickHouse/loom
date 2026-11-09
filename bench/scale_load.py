"""Memory at scale: load synthetic memories straight into the stores, then measure.

Offline and free: no provider call, the fake embedder's dimensions. `load-ch` writes
rows shaped like a `memory.set` into ClickHouse `loom.memory` (whose materialized view
fills `loom.memory_versions`) and `loom.memory_embeddings`, generated inside ClickHouse
(`INSERT … SELECT FROM numbers()`), so a load of millions takes minutes and no client
memory. `load-pg` copies those rows into the Postgres authority's `loom.memory_current`
and `loom.memory_revisions`, streamed from ClickHouse in the shape
`server.memory.store.pg_memory.encode_row` writes. `measure` then drives the real ops
through a running server and reads what the stores report: parts, part-log rates,
the vector read's bytes against its part count, table and index sizes, the time of a
user erasure and an org erasure, a vacuum and a `pg_dump` of one org.

Two shapes:

- `single`: every key in one org and one namespace;
- `zipf`: `--tenants` namespaces over `--orgs` orgs, sized by a Zipf law of exponent 1
  (tenant `t` of `T` holds about `1 / ((t + 1) ln T)` of the rows).

Each key gets 1 to 4 versions (2.4 on average, as the reference deployment's history);
the head is the newest. `created_at` spreads over `--months` months, so the memory
tables get one partition per month. Rows land under org names that start with
`scale-`; point the harness only at a ClickHouse and a Postgres of your own.

    PYTHONPATH=$PWD .venv/bin/python bench/scale_load.py load-ch --shape single --rows 1000000
    PYTHONPATH=$PWD .venv/bin/python bench/scale_load.py load-pg
    PYTHONPATH=$PWD .venv/bin/python bench/scale_load.py measure --base-url http://localhost:7790 \\
        --shape single --out bench/results/scale/single-1m.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from pathlib import Path
from typing import Any

import httpx

WORDS = (
    "alpha amber anchor apple arch atlas audit autumn badge basil beacon berry birch "
    "bridge bronze cabin canal candle canyon carbon cedar chalk cherry cipher citrus "
    "cliff cloud cobalt comet copper coral cotton crane crystal delta desert dial "
    "dune eagle echo ember engine falcon fern field flint forest fossil garden garnet "
    "glacier granite harbor hazel helium heron hollow honey horizon indigo iron island "
    "ivory jade jasper juniper kernel kettle lagoon lantern laurel lemon linen lotus "
    "magnet maple marble meadow meteor mint mirror mosaic nectar nickel north oasis "
    "ocean olive onyx orbit orchid oyster pebble pepper pine planet plaza pollen prism "
    "quartz quill radar raven reef ridge river rocket saffron sage salmon sapphire "
    "shadow signal silver slate solar spruce summit sunset thistle thunder timber "
    "topaz tulip tundra valley velvet violet walnut willow winter zephyr"
).split()
SINGLE_ORG = "scale-one"
SINGLE_NS = "big"
EMBED_MODEL = "fake-ngram-3072"
EMBED_DIM = 3072
# The base of every generated created_at: the months run back from here.
BASE_TS = 1_790_000_000
MONTH_S = 30 * 86400


def _env(path: str) -> dict[str, str]:
    from dotenv import dotenv_values

    out = (
        {k: v for k, v in dotenv_values(path).items() if v is not None}
        if Path(path).exists()
        else {}
    )
    out.update({k: v for k, v in os.environ.items() if k.startswith("LOOM_")})
    return out


def ch_client(env: dict[str, str]) -> Any:
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=env.get("LOOM_CH_HOST", "localhost"),
        port=int(env.get("LOOM_CH_PORT", "8123")),
        username=env.get("LOOM_CH_USER", "loom"),
        password=env.get("LOOM_CH_PASSWORD", "loomdev"),
        database="loom",
        send_receive_timeout=3600,
        settings={"max_execution_time": 0},
    )


def _words_literal() -> str:
    return "[" + ",".join(f"'{w}'" for w in WORDS) + "]"


def tenant_sql(shape: str, tenants: int, orgs: int) -> tuple[str, str]:
    """(org, namespace) expressions of the key number `n`."""
    if shape == "single":
        return f"'{SINGLE_ORG}'", f"'{SINGLE_NS}'"
    # A Weyl sequence spread through T^u - 1: log-uniform tenant ranks, Zipf of exponent 1.
    t = f"least(toUInt32(floor(pow({tenants}, (n * 0.6180339887498949) % 1))) - 1, {tenants - 1})"
    per_org = max(1, math.ceil(tenants / orgs))
    return (
        f"concat('scale-z', toString(intDiv({t}, {per_org})))",
        f"concat('ns', toString({t} % {per_org}))",
    )


def _key_cte(
    shape: str,
    tenants: int,
    orgs: int,
    months: int,
    start: int,
    count: int,
    agents: int,
    users: int,
) -> str:
    org, ns = tenant_sql(shape, tenants, orgs)
    user = f"concat('u', toString(cityHash64(n, 'user') % {users}))" if users else "'-'"
    words = _words_literal()
    nw = len(WORDS)
    # versions per key: n % 5 -> 4, 3, 2, 2, 1 (2.4 on average)
    return f"""
    WITH {words} AS w
    SELECT
        number AS n,
        {org} AS org,
        {ns} AS namespace,
        concat('agent', toString(n % {agents})) AS agent,
        {user} AS user_id,
        '-' AS session_id,
        concat('k/', toString(n)) AS memory_key,
        reinterpretAsUUID(reverse(unhex(substring(hex(MD5(toString(n))), 1, 32)))) AS memory_id,
        toDateTime({BASE_TS} - (n * 7919) % ({months} * {MONTH_S})) AS created_at,
        arrayElement([4, 3, 2, 2, 1], 1 + n % 5) AS nversions,
        arrayStringConcat(arrayMap(j -> w[1 + (n * (j * 2 + 3) + j * 31) % {nw}], range(14)), ' ')
            AS text
    FROM numbers({start}, {count})
    """


def load_ch(args: argparse.Namespace) -> None:
    ch = ch_client(_env(args.env))
    batch = args.batch
    t0 = time.monotonic()
    for start in range(args.offset, args.offset + args.rows, batch):
        count = min(batch, args.offset + args.rows - start)
        keys = _key_cte(
            args.shape, args.tenants, args.orgs, args.months, start, count, args.agents, args.users
        )
        # One row per version into loom.memory; memory_versions_mv copies each.
        ch.command(
            f"""
            INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key,
                memory_type, value, version, created_at, updated_at, last_reinforced_at,
                memory_id, status, content_sha256, source_class, evidence_count)
            SELECT org, namespace, agent, user_id, session_id, memory_key, 'semantic',
                concat('"', text, if(v = nversions, '', concat(' v', toString(v))), '"') AS value,
                toUInt64(toUnixTimestamp(created_at)) * 1000000000 + v * 1000 AS version,
                created_at + v * 60, created_at + v * 60, created_at + v * 60,
                memory_id, 'active', lower(hex(SHA256(value))), 'asserted', 1
            FROM ({keys}) ARRAY JOIN range(1, nversions + 1) AS v
            """,
            settings={"max_insert_threads": 8, "max_threads": 16},
        )
        # The head's vectors; one row per key (the sort key holds no version).
        ch.command(
            f"""
            INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,
                memory_key, memory_type, scope, content_excerpt, embedding_model, embedding_dim,
                embedding, created_at, version, status, memory_key_embedding,
                memory_key_embedding_dim, last_reinforced_at, source_class, content_sha256)
            SELECT org, namespace, agent, user_id, session_id, memory_key, 'semantic', 'private',
                concat('"', text, '" (#', memory_key, ')'), '{EMBED_MODEL}', {EMBED_DIM},
                arrayMap(j -> toBFloat16(randNormal(0, 1)), range({EMBED_DIM})),
                created_at + nversions * 60,
                toUInt64(toUnixTimestamp(created_at)) * 1000000000 + nversions * 1000,
                'active',
                arrayWithConstant({EMBED_DIM}, toBFloat16(0.01)), {EMBED_DIM},
                created_at + nversions * 60, 'asserted',
                lower(hex(SHA256(concat('"', text, '"'))))
            FROM ({keys})
            """,
            settings={"max_insert_threads": 8, "max_threads": 16},
        )
        done = start + count - args.offset
        rate = done / (time.monotonic() - t0)
        print(f"loaded {done:,}/{args.rows:,} keys ({rate:,.0f}/s)", flush=True)
        if _free_gb() < args.min_free_gb:
            raise SystemExit(f"free disk below {args.min_free_gb} GB; stopped at {done:,} keys")


def _free_gb() -> float:
    st = os.statvfs("/")
    return st.f_bavail * st.f_frsize / 1e9


# --- Postgres -------------------------------------------------------------------------


def pg_dsn(env: dict[str, str]) -> str:
    dsn = env.get("LOOM_PG_DSN", "")
    if not dsn:
        raise SystemExit("LOOM_PG_DSN is not set")
    return dsn


def _rows_from_ch(ch: Any, sql: str, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
    with ch.query_rows_stream(sql, parameters=params) as stream:
        names = stream.source.column_names
        for row in stream:
            yield dict(zip(names, row, strict=True))


def load_pg(args: argparse.Namespace) -> None:
    asyncio.run(_load_pg(args))


async def _load_pg(args: argparse.Namespace) -> None:
    """Every version (`loom.memory_versions`) into memory_revisions, per (org,
    namespace), numbered by version as commits would have numbered them; each key's
    newest revision into memory_current; then the scope generations."""
    import asyncpg

    from server.memory.store.pg_memory import encode_row

    env = _env(args.env)
    ch = ch_client(env)
    scopes = ch.query(
        "SELECT org, namespace, count() FROM loom.memory_versions "
        "WHERE startsWith(org, 'scale-') GROUP BY org, namespace ORDER BY org, namespace"
    ).result_rows
    t0 = time.monotonic()
    done = 0
    pg = await asyncpg.connect(pg_dsn(env))
    cols = (
        "org",
        "namespace",
        "seq",
        "user_id",
        "memory_id",
        "version",
        "content_sha256",
        "actor",
        "source_class",
        "request_id",
        "committed_at",
        "row",
        "audit",
    )
    for org, namespace, _n in scopes:
        seq = 0
        batch: list[tuple[Any, ...]] = []
        head_seq: dict[tuple[str, str, str, str], int] = {}
        async with pg.transaction():
            for r in _rows_from_ch(
                ch,
                "SELECT * EXCEPT (value_preview, value_len) FROM loom.memory_versions "
                "WHERE org = {o:String} AND namespace = {n:String} ORDER BY version",
                {"o": org, "n": namespace},
            ):
                seq += 1
                stored = encode_row(_json_ready(r))
                head_seq[
                    (stored["agent"], stored["user_id"], stored["session_id"], stored["memory_key"])
                ] = seq
                batch.append(
                    (
                        org,
                        namespace,
                        seq,
                        stored["user_id"],
                        uuid.UUID(stored["memory_id"]),
                        stored["version"],
                        stored["content_sha256"],
                        "",
                        "asserted",
                        "",
                        r["updated_at"].replace(tzinfo=UTC),
                        json.dumps(stored),
                        json.dumps(_audit(stored)),
                    )
                )
                if len(batch) >= 20_000:
                    await pg.copy_records_to_table(
                        "memory_revisions", schema_name="loom", records=batch, columns=cols
                    )
                    done += len(batch)
                    batch = []
            if batch:
                await pg.copy_records_to_table(
                    "memory_revisions", schema_name="loom", records=batch, columns=cols
                )
                done += len(batch)
            await pg.execute(
                """
                INSERT INTO loom.memory_current (org, namespace, agent, user_id, session_id,
                    memory_key, memory_id, version, seq, content_sha256, row)
                SELECT org, namespace, row->>'agent', user_id, row->>'session_id',
                    row->>'memory_key', memory_id, version, seq, content_sha256, row
                FROM loom.memory_revisions
                WHERE org = $1 AND namespace = $2 AND seq = ANY($3::bigint[])
                """,
                org,
                namespace,
                list(head_seq.values()),
            )
            await pg.execute(
                "INSERT INTO loom.scope_generations (org, scope, generation) VALUES ($1, $2, $3) "
                "ON CONFLICT (org, scope) DO UPDATE SET generation = EXCLUDED.generation",
                org,
                namespace,
                seq,
            )
        if seq >= 100_000 or len(scopes) < 50:
            print(
                f"{org}/{namespace}: {seq:,} revisions ({done / (time.monotonic() - t0):,.0f}/s)",
                flush=True,
            )
    await pg.close()
    print(
        f"loaded {done:,} revisions of {len(scopes):,} namespaces in {time.monotonic() - t0:,.0f} s"
    )


def _json_ready(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["value_json"] = out.get("value_json") or {}
    if isinstance(out.get("content_sha256"), bytes):
        out["content_sha256"] = out["content_sha256"].decode()
    return out


def _audit(stored: dict[str, Any]) -> dict[str, Any]:
    """A hash-only audit row like a commit's (`memory.revision`)."""
    return {
        "event_id": str(uuid.uuid4()),
        "ts": stored["updated_at"],
        "org": stored["org"],
        "namespace": stored["namespace"],
        "agent": stored["agent"],
        "actor": "",
        "operation": "memory.set",
        "memory_key": stored["memory_key"],
        "memory_id": stored["memory_id"],
        "content_sha256": stored["content_sha256"],
        "version": stored["version"],
        "user_id": stored["user_id"],
        "session_id": stored["session_id"],
    }


# --- measure ---------------------------------------------------------------------------


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))]


def _summary(lat: list[float], errors: dict[str, int]) -> dict[str, Any]:
    return {
        "n": len(lat),
        "p50_ms": round(_pct(lat, 0.50), 1),
        "p99_ms": round(_pct(lat, 0.99), 1),
        "mean_ms": round(statistics.fmean(lat), 1) if lat else 0.0,
        "errors": errors,
    }


def sample_keys(ch: Any, shape: str, n: int) -> list[dict[str, Any]]:
    """`n` existing heads to read, spread over the load (largest tenant for single)."""
    where = (
        "org = {o:String} AND namespace = {ns:String}"
        if shape == "single"
        else "startsWith(org, 'scale-')"
    )
    rows = ch.query(
        f"SELECT org, namespace, agent, user_id, memory_key FROM loom.memory_embeddings "
        f"WHERE {where} ORDER BY rand() LIMIT {int(n)}",
        parameters={"o": SINGLE_ORG, "ns": SINGLE_NS},
    ).result_rows
    return [
        dict(zip(("org", "namespace", "agent", "user_id", "memory_key"), r, strict=True))
        for r in rows
    ]


def biggest_tenants(ch: Any, k: int) -> list[tuple[str, str, int]]:
    return [
        (r[0], r[1], int(r[2]))
        for r in ch.query(
            "SELECT org, namespace, count() c FROM loom.memory_embeddings "
            "WHERE startsWith(org, 'scale-') GROUP BY org, namespace "
            "ORDER BY c DESC LIMIT {k:UInt32}",
            parameters={"k": k},
        ).result_rows
    ]


def drive(
    base_url: str, calls: list[tuple[str, dict[str, Any]]], concurrency: int, timeout: float
) -> dict[str, Any]:
    lat: list[float] = []
    errors: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    with httpx.Client(base_url=base_url, timeout=timeout) as http:

        def one(call: tuple[str, dict[str, Any]]) -> None:
            path, body = call
            t = time.perf_counter()
            try:
                r = http.post(path, json=body)
                ms = (time.perf_counter() - t) * 1000
                if r.status_code == 200:
                    lat.append(ms)
                    for o in _outcomes(path, r.json()):
                        outcomes[o] = outcomes.get(o, 0) + 1
                else:
                    code = f"{r.status_code}:{_error_code(r)}"
                    errors[code] = errors.get(code, 0) + 1
            except httpx.HTTPError as e:
                errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1

        with ThreadPoolExecutor(concurrency) as pool:
            list(pool.map(one, calls))
    return {**_summary(lat, errors), "outcomes": outcomes}


def _error_code(r: httpx.Response) -> str:
    """The Loom error code of a refused call (`{"detail": {"code": ...}}`), or ''."""
    try:
        body = r.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail.get("code", "")) if isinstance(detail, dict) else ""


def _outcomes(path: str, body: dict[str, Any]) -> list[str]:
    """What a write did: a new row, a no-op, a duplicate, an unchecked entry."""
    if path.endswith("memory.set"):
        if body.get("noop"):
            return ["noop"]
        if body.get("duplicate_of") or body.get("merged_into_key"):
            return ["duplicate"]
        return ["written"]
    if path.endswith("memory.bulk_set"):
        out = ["written"] * len(body.get("written") or [])
        out += ["duplicate"] * len(body.get("duplicates") or [])
        out += ["merged"] * len(body.get("merged") or [])
        out += ["unchecked"] * int(body.get("unchecked") or 0)
        return out
    if path.endswith("memory.search"):
        return ["hits"] * len(body.get("results") or [])
    return []


def _text(rng: random.Random, n: int = 14) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def op_calls(
    op: str, keys: list[dict[str, Any]], n: int, rng: random.Random
) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for i in range(n):
        k = keys[i % len(keys)]
        base = {
            "org": k["org"],
            "namespace": k["namespace"],
            "agent": k["agent"],
            "user_id": k["user_id"],
        }
        if op == "memory.get":
            out.append(("/v1/memory.get", {**base, "memory_key": k["memory_key"]}))
        elif op == "memory.set":
            out.append(
                (
                    "/v1/memory.set",
                    {
                        **base,
                        "memory_key": f"new/{uuid.uuid4().hex[:12]}",
                        "memory_type": "semantic",
                        "value": _text(rng),
                    },
                )
            )
        elif op == "memory.bulk_set":
            out.append(
                (
                    "/v1/memory.bulk_set",
                    {
                        **base,
                        "entries": [
                            {
                                "memory_key": f"bulk/{uuid.uuid4().hex[:12]}",
                                "memory_type": "semantic",
                                "value": _text(rng),
                            }
                            for _ in range(20)
                        ],
                    },
                )
            )
        elif op == "memory.search":
            out.append(("/v1/memory.search", {**base, "query": _text(rng, 6), "top_k": 10}))
    return out


def parts(ch: Any) -> dict[str, Any]:
    rows = ch.query(
        "SELECT table, count(), sum(rows), sum(bytes_on_disk), sum(data_uncompressed_bytes) "
        "FROM system.parts WHERE database = 'loom' AND active "
        "AND table IN ('memory', 'memory_embeddings', 'memory_versions') GROUP BY table"
    ).result_rows
    return {r[0]: {"parts": r[1], "rows": r[2], "bytes": r[3], "uncompressed": r[4]} for r in rows}


def new_parts_per_s(ch: Any, since: float, until: float) -> dict[str, float]:
    rows = ch.query(
        "SELECT table, count() FROM system.part_log WHERE database = 'loom' "
        "AND event_type = 'NewPart' "
        "AND table IN ('memory', 'memory_embeddings', 'memory_versions') "
        "AND event_time_microseconds BETWEEN fromUnixTimestamp64Micro({a:Int64}) "
        "AND fromUnixTimestamp64Micro({b:Int64}) GROUP BY table",
        parameters={"a": int(since * 1e6), "b": int(until * 1e6)},
    ).result_rows
    span = max(1e-9, until - since)
    return {r[0]: round(r[1] / span, 2) for r in rows}


def op_reads(ch: Any, op: str, since: float, until: float, calls: int) -> dict[str, Any]:
    """What the op's ClickHouse statements read in the window, per call, and the largest
    statement's memory."""
    r = ch.query(
        "SELECT count(), sum(read_bytes), max(memory_usage), countIf(type != 'QueryFinish') "
        "FROM system.query_log WHERE type != 'QueryStart' AND startsWith(log_comment, {op:String}) "
        "AND event_time_microseconds BETWEEN fromUnixTimestamp64Micro({a:Int64}) "
        "AND fromUnixTimestamp64Micro({b:Int64})",
        parameters={"op": op + " ", "a": int(since * 1e6), "b": int(until * 1e6)},
    ).result_rows[0]
    n = max(1, calls)
    return {
        "statements_per_call": round(int(r[0]) / n, 1),
        "read_mib_per_call": round(int(r[1] or 0) / n / 2**20, 1),
        "max_statement_memory_mib": round(int(r[2] or 0) / 2**20, 1),
        "failed_statements": int(r[3]),
    }


def vector_read(
    base_url: str, ch: Any, key: dict[str, Any], reps: int, rng: random.Random
) -> dict[str, Any]:
    """`reps` memory.search calls in one tenant (the identity of `key`), one at a time:
    their latency, and from system.query_log what the vector leg's statement read."""
    t0 = time.time()
    res = drive(base_url, op_calls("memory.search", [key], reps, rng), 1, 120)
    t1 = time.time()
    ch.command("SYSTEM FLUSH LOGS")
    log = ch.query(
        "SELECT count(), quantile(0.5)(query_duration_ms), max(query_duration_ms), avg(read_rows), "
        "avg(read_bytes), avg(memory_usage) FROM system.query_log "
        "WHERE type = 'QueryFinish' AND startsWith(log_comment, 'memory.search') "
        "AND position(query, 'cosineDistance(e.embedding') > 0 "
        "AND event_time_microseconds BETWEEN fromUnixTimestamp64Micro({a:Int64}) "
        "AND fromUnixTimestamp64Micro({b:Int64})",
        parameters={"a": int(t0 * 1e6), "b": int(t1 * 1e6)},
    ).result_rows[0]
    return {
        "search": res,
        "vector_statements": int(log[0]),
        "vector_p50_ms": float(log[1] or 0),
        "vector_max_ms": float(log[2] or 0),
        "read_rows": int(log[3] or 0),
        "read_mib": round((log[4] or 0) / 2**20, 1),
        "memory_mib": round((log[5] or 0) / 2**20, 1),
    }


def probe_tenants(ch: Any, shape: str) -> list[dict[str, Any]]:
    """The identities a vector probe searches as: the single namespace, or one key's
    identity in each of the three biggest tenants."""
    tenants = [(SINGLE_ORG, SINGLE_NS, 0)] if shape == "single" else biggest_tenants(ch, 3)
    out = []
    for org, ns, rows in tenants:
        r = ch.query(
            "SELECT agent, user_id FROM loom.memory_embeddings WHERE org = {o:String} "
            "AND namespace = {n:String} LIMIT 1",
            parameters={"o": org, "n": ns},
        ).result_rows[0]
        out.append({"org": org, "namespace": ns, "agent": r[0], "user_id": r[1], "rows": rows})
    return out


def tenant_parts(ch: Any, org: str, ns: str) -> int:
    # Parts holding the tenant: one per partition at most after OPTIMIZE FINAL.
    return int(
        ch.query(
            "SELECT uniqExact(_part) FROM loom.memory_embeddings "
            "WHERE org = {o:String} AND namespace = {n:String}",
            parameters={"o": org, "n": ns},
        ).result_rows[0][0]
    )


def _pg_run(env: dict[str, str], fn: Any) -> Any:
    import asyncpg

    async def go() -> Any:
        conn = await asyncpg.connect(pg_dsn(env))
        try:
            return await fn(conn)
        finally:
            await conn.close()

    return asyncio.run(go())


def pg_sizes(env: dict[str, str]) -> dict[str, Any]:
    async def go(pg: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for t in ("memory_current", "memory_revisions", "memory_deliveries"):
            heap, toast, idx = await pg.fetchrow(
                "SELECT pg_relation_size(c.oid), "
                "coalesce(pg_total_relation_size(c.reltoastrelid), 0), pg_indexes_size(c.oid) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'loom' AND c.relname = $1",
                t,
            )
            rows = await pg.fetchval(f"SELECT count(*) FROM loom.{t}")
            out[t] = {
                "rows": rows,
                "heap": heap,
                "toast": toast,
                "indexes": idx,
                "per_row": round((heap + toast + idx) / rows, 1) if rows else 0,
            }
        return out

    sizes: dict[str, Any] = _pg_run(env, go)
    return sizes


def pg_vacuum_s(env: dict[str, str], table: str) -> float:
    async def go(pg: Any) -> float:
        t = time.monotonic()
        await pg.execute(f"VACUUM (ANALYZE) loom.{table}")
        return round(time.monotonic() - t, 2)

    seconds: float = _pg_run(env, go)
    return seconds


def pg_dump_org_s(container: str, org: str) -> dict[str, float]:
    """`COPY (… WHERE org = …) TO STDOUT` of the org's rows of every memory table,
    the dump of one tenant out of a shared schema; seconds and bytes."""
    t = time.monotonic()
    total = 0
    for table in ("memory_current", "memory_revisions", "scope_generations"):
        out = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                "loom",
                "-d",
                "loom",
                "-Atc",
                f"COPY (SELECT * FROM loom.{table} WHERE org = '{org}') TO STDOUT",
            ],
            check=True,
            capture_output=True,
        ).stdout
        total += len(out)
    return {"seconds": round(time.monotonic() - t, 2), "bytes": total}


def measure(args: argparse.Namespace) -> None:
    env = _env(args.env)
    ch = ch_client(env)
    rng = random.Random()  # fresh values each run: a repeated value is a duplicate, not a write
    health = httpx.get(f"{args.base_url}/v1/health", timeout=10).json()
    result: dict[str, Any] = {
        "shape": args.shape,
        "authority": health.get("storage_authority"),
        "build": health.get("build_commit"),
        "parts_before": parts(ch),
    }
    keys = sample_keys(ch, args.shape, 2000)
    ops: dict[str, Any] = {}
    for op in args.ops.split(","):
        n = args.calls if op != "memory.bulk_set" else max(20, args.calls // 10)
        calls = op_calls(op, keys, n, rng)
        t0 = time.time()
        ops[op] = drive(args.base_url, calls, args.concurrency, args.timeout)
        t1 = time.time()
        ops[op]["seconds"] = round(t1 - t0, 1)
        ch.command("SYSTEM FLUSH LOGS")
        ops[op]["new_parts_per_s"] = new_parts_per_s(ch, t0, t1)
        ops[op]["clickhouse"] = op_reads(ch, op, t0, t1, ops[op]["n"])
        ops[op]["host_load_1m"] = round(os.getloadavg()[0], 1)
        print(op, json.dumps(ops[op]), flush=True)
        result["ops"] = ops
        _save(args.out, result)
    result["vector"] = []
    for key in probe_tenants(ch, args.shape):
        result["vector"].append(
            {
                **key,
                "parts": tenant_parts(ch, key["org"], key["namespace"]),
                **vector_read(args.base_url, ch, key, args.vector_reps, rng),
            }
        )
        print("vector", json.dumps(result["vector"][-1]), flush=True)
        _save(args.out, result)
    result["parts_after"] = parts(ch)
    if env.get("LOOM_PG_DSN"):
        result["pg"] = pg_sizes(env)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, default=str))
    print(f"wrote {args.out}")


def _save(path: str, result: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(result, indent=2, default=str))


def optimize(args: argparse.Namespace) -> None:
    """Vector reads against part count: before, then after OPTIMIZE FINAL of the table."""
    env = _env(args.env)
    ch = ch_client(env)
    rng = random.Random(11)
    tenants = probe_tenants(ch, args.shape)
    out: dict[str, Any] = {"before": [], "after": []}
    for phase in ("before", "after"):
        if phase == "after":
            t = time.monotonic()
            ch.command("OPTIMIZE TABLE loom.memory_embeddings FINAL", settings={"alter_sync": 2})
            out["optimize_s"] = round(time.monotonic() - t, 1)
        for key in tenants:
            out[phase].append(
                {
                    **key,
                    "parts": tenant_parts(ch, key["org"], key["namespace"]),
                    **vector_read(args.base_url, ch, key, args.vector_reps, rng),
                }
            )
            print(phase, json.dumps(out[phase][-1]), flush=True)
    out["table_parts"] = parts(ch)
    Path(args.out).write_text(json.dumps(out, indent=2))
    print(f"wrote {args.out}")


def erase(args: argparse.Namespace) -> None:
    """Time a user erasure and an org erasure through the server's own ops."""
    env = _env(args.env)
    ch = ch_client(env)
    out: dict[str, Any] = {}
    with httpx.Client(base_url=args.base_url, timeout=3600) as http:
        if args.user:
            org, user = args.user.split("/", 1)
            n = ch.query(
                "SELECT count() FROM loom.memory_embeddings "
                "WHERE org = {o:String} AND user_id = {u:String}",
                parameters={"o": org, "u": user},
            ).result_rows[0][0]
            t = time.monotonic()
            r = http.post(
                "/v1/erasure.request",
                json={
                    "org": org,
                    "scope_type": "user_id",
                    "scope_value": user,
                    "requester": "scale-bench",
                    "purge": True,
                },
            )
            out["user"] = {
                "org": org,
                "user_id": user,
                "keys": n,
                "status": r.status_code,
                "seconds": round(time.monotonic() - t, 2),
                "body": r.json() if r.content else None,
            }
            print("user", json.dumps(out["user"], default=str)[:2000], flush=True)
        if args.org:
            nss = [
                r[0]
                for r in ch.query(
                    "SELECT DISTINCT namespace FROM loom.memory_embeddings WHERE org = {o:String}",
                    parameters={"o": args.org},
                ).result_rows
            ]
            n = ch.query(
                "SELECT count() FROM loom.memory_embeddings WHERE org = {o:String}",
                parameters={"o": args.org},
            ).result_rows[0][0]
            # An org's erasure: every namespace it holds, in one saga.
            t = time.monotonic()
            r = http.post(
                "/v1/erasure.request",
                json={
                    "org": args.org,
                    "scope_type": "namespace_list",
                    "scope_value": json.dumps({"namespaces": nss, "purge": True}),
                    "requester": "scale-bench",
                    "purge": True,
                    "external": False,
                },
            )
            out["org"] = {
                "org": args.org,
                "namespaces": len(nss),
                "keys": n,
                "status": r.status_code,
                "seconds": round(time.monotonic() - t, 2),
                "body": r.json() if r.content else None,
            }
            print("org", json.dumps(out["org"], default=str)[:2000], flush=True)
    Path(args.out).write_text(json.dumps(out, indent=2, default=str))


def pg_ops(args: argparse.Namespace) -> None:
    env = _env(args.env)
    out = {"sizes": pg_sizes(env)}
    out["vacuum_s"] = {t: pg_vacuum_s(env, t) for t in ("memory_current", "memory_revisions")}
    if args.dump_org:
        out["dump_org"] = {"org": args.dump_org, **pg_dump_org_s(args.container, args.dump_org)}
    print(json.dumps(out, indent=2))
    Path(args.out).write_text(json.dumps(out, indent=2))


def set_authority(args: argparse.Namespace) -> None:
    """Record `--to` as this private deployment's authority, in ClickHouse and in
    Postgres under one deployment id, so a server of either authority boots on the rows
    loaded. Only for a ClickHouse and a Postgres of the harness's own: a deployment's
    authority is recorded once and never switched this way."""
    env = _env(args.env)
    ch = ch_client(env)
    rows = ch.query("SELECT deployment_id FROM loom.storage_authority FINAL").result_rows
    deployment = rows[0][0] if rows else str(uuid.uuid4())
    if args.to == "postgres":

        async def record(pg: Any) -> None:
            await pg.execute("DELETE FROM loom.authority")
            await pg.execute(
                "INSERT INTO loom.authority (authority, epoch, deployment_id) "
                "VALUES ('postgres', 1, $1)",
                deployment,
            )

        _pg_run(env, record)
    ch.command("TRUNCATE TABLE loom.storage_authority")
    ch.command(
        "INSERT INTO loom.storage_authority (authority, epoch, deployment_id) "
        "VALUES ({a:String}, 1, {d:String})",
        parameters={"a": args.to, "d": deployment},
    )
    print(f"recorded {args.to} for deployment {deployment}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--env", default=".env")
    sub = p.add_subparsers(dest="cmd", required=True)
    lc = sub.add_parser("load-ch")
    lc.add_argument("--shape", choices=("single", "zipf"), default="single")
    lc.add_argument("--rows", type=int, required=True, help="keys to load")
    lc.add_argument("--offset", type=int, default=0, help="first key number (to grow a load)")
    lc.add_argument("--tenants", type=int, default=10000)
    lc.add_argument("--orgs", type=int, default=1000)
    lc.add_argument("--months", type=int, default=12)
    lc.add_argument("--agents", type=int, default=1, help="agents per tenant")
    lc.add_argument(
        "--users", type=int, default=0, help="users per tenant (0: every row's user_id is '-')"
    )
    lc.add_argument("--batch", type=int, default=250_000)
    lc.add_argument("--min-free-gb", type=float, default=15.0)
    sub.add_parser("load-pg")
    m = sub.add_parser("measure")
    m.add_argument("--base-url", required=True)
    m.add_argument("--shape", choices=("single", "zipf"), default="single")
    m.add_argument("--ops", default="memory.get,memory.set,memory.bulk_set,memory.search")
    m.add_argument("--calls", type=int, default=400)
    m.add_argument("--concurrency", type=int, default=5)
    m.add_argument("--timeout", type=float, default=120)
    m.add_argument("--vector-reps", type=int, default=20)
    m.add_argument("--out", required=True)
    o = sub.add_parser("optimize")
    o.add_argument("--base-url", required=True)
    o.add_argument("--shape", choices=("single", "zipf"), default="single")
    o.add_argument("--vector-reps", type=int, default=10)
    o.add_argument("--out", required=True)
    e = sub.add_parser("erase")
    e.add_argument("--base-url", required=True)
    e.add_argument("--user", help="org/user_id")
    e.add_argument("--org")
    e.add_argument("--out", required=True)
    g = sub.add_parser("pg")
    g.add_argument("--dump-org")
    g.add_argument("--container", default="loom-scale-pg")
    g.add_argument("--out", required=True)
    a = sub.add_parser("authority", help="switch this private deployment's recorded authority")
    a.add_argument("--to", choices=("clickhouse", "postgres"), required=True)
    args = p.parse_args(argv)
    {
        "load-ch": load_ch,
        "load-pg": load_pg,
        "measure": measure,
        "optimize": optimize,
        "erase": erase,
        "pg": pg_ops,
        "authority": set_authority,
    }[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
