"""Memory at scale: load synthetic memories straight into the stores, then measure.

Offline and free: no provider call, the fake embedder's dimensions. `load-ch` writes
rows shaped like a `memory.set` into ClickHouse `loom.memory` (whose materialized view
fills `loom.memory_versions`) and `loom.memory_embeddings`, generated inside ClickHouse
(`INSERT … SELECT FROM numbers()`), so a load of millions takes minutes and no client
memory. `load-pg` copies those rows into the Postgres authority's `loom.memory_current`
and `loom.memory_revisions`, streamed from ClickHouse in the shape
`server.memory.store.pg_memory.encode_row` writes. `measure` then drives the real ops
through a running server and reads what the stores report: parts, part-log rates,
the vector read's bytes against its part count, table and index sizes. `erase` times a
user erasure and an org erasure; `pg` times a vacuum and a `COPY` of one org's rows.

Two shapes:

- `single`: every key in one org and one namespace;
- `zipf`: `--tenants` namespaces over `--orgs` orgs, sized by a Zipf law of exponent 1
  (tenant `t` of `T` holds `ln((t + 2) / (t + 1)) / ln(T + 1)` of the rows, about
  `1 / ((t + 1.5) ln T)`).

Each key gets 1 to 4 versions (2.4 on average, the harness's choice);
the head is the newest. `created_at` spreads over `--months` months, so the memory
tables get a partition for each month the window touches (13 for 12 months). Rows land
under org names that start with `scale-`; point the harness only at a ClickHouse and a
Postgres of your own.

    PYTHONPATH=$PWD .venv/bin/python bench/scale_load.py load-ch --shape single --rows 1000000
    PYTHONPATH=$PWD .venv/bin/python bench/scale_load.py load-pg
    PYTHONPATH=$PWD .venv/bin/python bench/scale_load.py measure --base-url http://localhost:7790 \\
        --shape single --out bench/out/scale/single-1m.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
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
    # A Weyl sequence spread through (T + 1)^u - 1: log-uniform tenant ranks 0 to T - 1,
    # Zipf of exponent 1.
    t = f"toUInt32(floor(pow({tenants + 1}, (n * 0.6180339887498949) % 1))) - 1"
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


def own_stores(ch: Any) -> None:
    """Refuse to touch a ClickHouse that holds any org's memory but the harness's own
    (`scale-…`): the harness loads, optimizes and rewrites the recorded authority."""
    other = ch.query(
        "SELECT count() FROM loom.memory WHERE NOT startsWith(org, 'scale-')"
    ).result_rows[0][0]
    if other:
        raise SystemExit(
            f"this ClickHouse holds {other} memory rows of other orgs: point the harness "
            "only at stores of its own"
        )


async def own_pg_stores(pg: Any) -> None:
    """`own_stores` for the Postgres authority's tables: no head of another org."""
    other = await pg.fetchval(
        "SELECT count(*) FROM loom.memory_current WHERE org NOT LIKE 'scale-%'"
    )
    if other:
        raise SystemExit(
            f"this Postgres holds {other} memory heads of other orgs: point the harness "
            "only at stores of its own"
        )


def load_ch(args: argparse.Namespace) -> None:
    ch = ch_client(_env(args.env))
    own_stores(ch)
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
    own_stores(ch)
    scopes = ch.query(
        "SELECT DISTINCT org, namespace FROM loom.memory_versions "
        "WHERE startsWith(org, 'scale-') ORDER BY org, namespace"
    ).result_rows
    t0 = time.monotonic()
    done = 0
    pg = await asyncpg.connect(pg_dsn(env))
    await own_pg_stores(pg)
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
    for org, namespace in scopes:
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
    """Send `calls` from `concurrency` threads; the successful calls' latency, the refused
    ones by status and Loom error code, and what the writes did."""
    with httpx.Client(base_url=base_url, timeout=timeout) as http:

        def one(call: tuple[str, dict[str, Any]]) -> tuple[float | None, str, list[str]]:
            path, body = call
            t = time.perf_counter()
            try:
                r = http.post(path, json=body)
            except httpx.HTTPError as e:
                return None, type(e).__name__, []
            ms = (time.perf_counter() - t) * 1000
            if r.status_code != 200:
                return None, f"{r.status_code}:{_error_code(r)}", []
            return ms, "", _outcomes(path, r.json())

        with ThreadPoolExecutor(concurrency) as pool:
            done = list(pool.map(one, calls))
    lat = [ms for ms, _e, _o in done if ms is not None]
    errors = Counter(e for _ms, e, _o in done if e)
    outcomes = Counter(o for _ms, _e, os_ in done for o in os_)
    return {**_summary(lat, dict(errors)), "outcomes": dict(outcomes)}


def _error_code(r: httpx.Response) -> str:
    """The Loom error code of a refused call (`{"detail": {"code": ...}}`), or ''."""
    try:
        body = r.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail.get("code", "")) if isinstance(detail, dict) else ""


def _outcomes(path: str, body: dict[str, Any]) -> list[str]:
    """What a write did, as tags: `memory.set` gets one (a new row, a no-op or a
    duplicate); a `memory.bulk_set` entry that wrote a row is `written`, and may also be
    `duplicate` (stored as a duplicate), `merged` or `unchecked` (written without its
    check), so a bulk call's tags can outnumber its entries."""
    if path.endswith("memory.set"):
        if body.get("noop"):
            return ["noop"]
        if body.get("duplicate_of") or body.get("merged_into_key"):
            return ["duplicate"]
        return ["written"]
    if path.endswith("memory.bulk_set"):
        # `written` holds one result per entry; an entry that wrote no row has version 0.
        out = ["written"] * sum(1 for w in body.get("written") or [] if int(w.get("version") or 0))
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


def flush_logs(ch: Any, attempts: int = 10) -> None:
    """`SYSTEM FLUSH LOGS`, waiting out a ClickHouse that is short of memory right after a
    load (code 241) for up to `attempts` tries, 5 s apart."""
    for attempt in range(attempts):
        try:
            ch.command("SYSTEM FLUSH LOGS")
            return
        except Exception as e:  # waits out code 241 only; the rest re-raise
            if "Code: 241." not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(5)


def op_reads(
    ch: Any, op: str, since: float, until: float, calls: int, *, flushes: bool = False
) -> dict[str, Any]:
    """What the op's ClickHouse statements read and the CPU they used in the window, per
    call sent (refused calls' statements included), and the largest statement's memory.
    `flushes`: also the publication batch's statements in the window (under the Postgres
    authority a write's inserts are sent by it, tagged `pg_batch.flush`, not by the op)."""
    r = ch.query(
        "SELECT count(), sum(read_bytes), max(memory_usage), countIf(type != 'QueryFinish'), "
        "sum(ProfileEvents['OSCPUVirtualTimeMicroseconds']) "
        "FROM system.query_log WHERE type != 'QueryStart' "
        "AND (startsWith(log_comment, {op:String}) "
        "OR ({flushes:Bool} AND startsWith(log_comment, 'pg_batch.flush'))) "
        "AND event_time_microseconds BETWEEN fromUnixTimestamp64Micro({a:Int64}) "
        "AND fromUnixTimestamp64Micro({b:Int64})",
        parameters={
            "op": op + " ",
            "flushes": flushes,
            "a": int(since * 1e6),
            "b": int(until * 1e6),
        },
    ).result_rows[0]
    n = max(1, calls)
    return {
        "statements_per_call": round(int(r[0]) / n, 1),
        "read_mib_per_call": round(int(r[1] or 0) / n / 2**20, 1),
        "max_statement_memory_mib": round(int(r[2] or 0) / 2**20, 1),
        "failed_statements": int(r[3]),
        "clickhouse_cpu_ms_per_call": round(int(r[4] or 0) / n / 1000, 2),
    }


def vector_read(
    base_url: str, ch: Any, key: dict[str, Any], reps: int, rng: random.Random
) -> dict[str, Any]:
    """`reps` memory.search calls in one tenant (the identity of `key`), one at a time:
    their latency, and from system.query_log what the vector leg's statement read."""
    t0 = time.time()
    res = drive(base_url, op_calls("memory.search", [key], reps, rng), 1, 120)
    t1 = time.time()
    flush_logs(ch)
    log = ch.query(
        "SELECT count(), quantile(0.5)(query_duration_ms), max(query_duration_ms), avg(read_rows), "
        "avg(read_bytes), avg(memory_usage) FROM system.query_log "
        "WHERE type = 'QueryFinish' AND startsWith(log_comment, 'memory.search') "
        "AND position(query, 'cosineDistance(e.embedding') > 0 "
        "AND event_time_microseconds BETWEEN fromUnixTimestamp64Micro({a:Int64}) "
        "AND fromUnixTimestamp64Micro({b:Int64})",
        parameters={"a": int(t0 * 1e6), "b": int(t1 * 1e6)},
    ).result_rows[0]
    # The filter matches every leg that ranks `e.embedding` (the fact leg, the anchor pool,
    # the key-match arm); one statement a search means only the fact leg ran.
    return {
        "search": res,
        "vector_statements": int(log[0]),
        "only_the_fact_leg": int(log[0]) == reps,
        "vector_p50_ms": float(log[1] or 0),
        "vector_max_ms": float(log[2] or 0),
        "read_rows": int(log[3] or 0),
        "read_mib": round((log[4] or 0) / 2**20, 1),
        "memory_mib": round((log[5] or 0) / 2**20, 1),
    }


def probe_tenants(ch: Any, shape: str) -> list[dict[str, Any]]:
    """The identities a vector probe searches as: the single namespace, or one key's
    identity in each of the three biggest tenants."""
    tenants = biggest_tenants(ch, 3)
    if shape == "single":
        tenants = [t for t in tenants if t[:2] == (SINGLE_ORG, SINGLE_NS)]
    out = []
    for org, ns, rows in tenants:
        r = ch.query(
            "SELECT agent, user_id FROM loom.memory_embeddings WHERE org = {o:String} "
            "AND namespace = {n:String} LIMIT 1",
            parameters={"o": org, "n": ns},
        ).result_rows[0]
        scope = ch.query(
            "SELECT count() FROM loom.memory_embeddings WHERE org = {o:String} "
            "AND namespace = {n:String} AND agent = {a:String} AND user_id = {u:String}",
            parameters={"o": org, "n": ns, "a": r[0], "u": r[1]},
        ).result_rows[0][0]
        out.append(
            {
                "org": org,
                "namespace": ns,
                "agent": r[0],
                "user_id": r[1],
                "namespace_rows": rows,
                "scope_rows": int(scope),
            }
        )
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


def _pg_run[T](env: dict[str, str], fn: Callable[[Any], Awaitable[T]]) -> T:
    import asyncpg

    async def go() -> T:
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

    return _pg_run(env, go)


def pg_vacuum_s(env: dict[str, str], table: str) -> float:
    async def go(pg: Any) -> float:
        t = time.monotonic()
        await pg.execute(f"VACUUM (ANALYZE) loom.{table}")
        return round(time.monotonic() - t, 2)

    return _pg_run(env, go)


def pg_dump_org_s(env: dict[str, str], org: str) -> dict[str, float]:
    """A `COPY (… WHERE org = $1) TO STDOUT` of the org's rows of every memory table,
    the dump of one tenant out of a shared schema, streamed and counted: seconds and
    bytes."""
    tables = ("memory_current", "memory_revisions", "scope_generations")

    async def go(pg: Any) -> dict[str, float]:
        total = 0

        async def count(chunk: bytes) -> None:
            nonlocal total
            total += len(chunk)

        t = time.monotonic()
        for table in tables:
            await pg.copy_from_query(
                f"SELECT * FROM loom.{table} WHERE org = $1", org, output=count
            )
        return {"seconds": round(time.monotonic() - t, 2), "bytes": total}

    return _pg_run(env, go)


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
    result["ops"] = ops
    for op in args.ops.split(","):
        n = args.calls if op != "memory.bulk_set" else max(20, args.calls // 10)
        calls = op_calls(op, keys, n, rng)
        t0 = time.time()
        ops[op] = drive(args.base_url, calls, args.concurrency, args.timeout)
        t1 = time.time()
        ops[op]["seconds"] = round(t1 - t0, 1)
        flush_logs(ch)
        ops[op]["new_parts_per_s"] = new_parts_per_s(ch, t0, t1)
        ops[op]["calls_sent"] = len(calls)
        writes = op in ("memory.set", "memory.bulk_set")
        ops[op]["clickhouse"] = op_reads(
            ch,
            op,
            t0,
            t1,
            len(calls),
            flushes=writes and result["authority"] == "postgres",
        )
        ops[op]["host_load_1m"] = round(os.getloadavg()[0], 1)
        print(op, json.dumps(ops[op]), flush=True)
        _save(args.out, result)  # after each op, so a run cut short keeps what it measured
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
    _save(args.out, result)
    print(f"wrote {args.out}")


def _save(path: str, result: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(result, indent=2, default=str))


def _partition_sample(ch: Any) -> dict[str, Any]:
    """The memory tables' most active parts in one partition, their active parts and the
    merges running, now."""
    rows = ch.query(
        "SELECT table, max(c), sum(c) FROM (SELECT table, partition, count() AS c "
        "FROM system.parts WHERE database = 'loom' AND active "
        "AND table IN ('memory', 'memory_embeddings', 'memory_versions') "
        "GROUP BY table, partition) GROUP BY table"
    ).result_rows
    merges = ch.query(
        "SELECT count() FROM system.merges WHERE database = 'loom' "
        "AND table IN ('memory', 'memory_embeddings', 'memory_versions')"
    ).result_rows[0][0]
    return {
        "parts": {r[0]: {"max_in_partition": r[1], "active": r[2]} for r in rows},
        "merges": int(merges),
    }


async def open_loop(
    total: int, rate: float, max_in_flight: int, send: Callable[[int], Awaitable[None]]
) -> tuple[int, float]:
    """Start `send(i)` for i < `total` at start + i / `rate`, whatever earlier sends are
    doing, with at most `max_in_flight` running; a send due while that many run is not
    started and is counted. Returns (not started, seconds the schedule took), once every
    started send has finished."""
    gate = asyncio.Semaphore(max_in_flight)
    skipped = 0

    async def run(i: int) -> None:
        try:
            await send(i)
        finally:
            gate.release()

    tasks = []
    start = time.monotonic()
    for i in range(total):
        delay = start + i / rate - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if gate.locked():  # at the in-flight cap: this send is not started, and counted
            skipped += 1
            continue
        await gate.acquire()
        tasks.append(asyncio.create_task(run(i)))
    took = time.monotonic() - start
    await asyncio.gather(*tasks)
    return skipped, took


def fleet(args: argparse.Namespace) -> None:
    """`memory.set` at `--rate` writes a second, open loop (write i is sent at
    start + i / rate whatever earlier writes are doing, up to `--max-in-flight` at once),
    spread round-robin over `--base-url`'s servers, for `--seconds`; then each memory
    table's new parts a second from `system.part_log`, the most active parts any partition
    reached and the merges in flight (sampled every second), against the tables'
    `parts_to_delay_insert` and `parts_to_throw_insert`. It reports the rate offered (writes
    sent a second) beside the rate served (successful writes a second)."""
    env = _env(args.env)
    ch = ch_client(env)
    urls = [u.strip() for u in args.base_url.split(",") if u.strip()]
    rng = random.Random()
    samples: list[dict[str, Any]] = []
    sample_errors: list[str] = []
    stop = threading.Event()

    def sample() -> None:
        sampler = ch_client(env)
        while not stop.is_set():
            try:
                samples.append(_partition_sample(sampler))
            except Exception as e:  # noqa: BLE001 — a missed sample is counted, not fatal
                sample_errors.append(type(e).__name__)
            stop.wait(1.0)

    async def drive_open_loop() -> tuple[list[float], Counter[str], int, float]:
        lat: list[float] = []
        errors: Counter[str] = Counter()
        clients = [httpx.AsyncClient(base_url=u, timeout=args.timeout) for u in urls]

        async def one(i: int) -> None:
            body = {
                "org": "scale-fleet",
                "namespace": f"ns{i % args.namespaces}",
                "agent": "agent0",
                "memory_key": f"fleet/{uuid.uuid4().hex[:12]}",
                "memory_type": "semantic",
                "value": _text(rng),
            }
            t = time.perf_counter()
            try:
                r = await clients[i % len(clients)].post("/v1/memory.set", json=body)
                if r.status_code == 200:
                    lat.append((time.perf_counter() - t) * 1000)
                else:
                    errors[f"{r.status_code}:{_error_code(r)}"] += 1
            except httpx.HTTPError as e:
                errors[type(e).__name__] += 1

        skipped, sent_for = await open_loop(
            int(args.rate * args.seconds), args.rate, args.max_in_flight, one
        )
        for c in clients:
            await c.aclose()
        return lat, errors, skipped, sent_for

    watcher = threading.Thread(target=sample, daemon=True)
    watcher.start()
    t0 = time.time()
    lat, errors, skipped, sent_for = asyncio.run(drive_open_loop())
    t1 = time.time()
    stop.set()
    watcher.join()
    # New parts a second come from system.part_log; a ClickHouse that cannot flush its
    # logs after the load still has the sampled parts and merges, so the run keeps those.
    try:
        flush_logs(ch)
        parts_per_s: dict[str, float] | str = new_parts_per_s(ch, t0, t1)
    except Exception as e:  # noqa: BLE001 — the writes and samples are already measured
        parts_per_s = f"not measured: logs not flushed ({str(e)[:160]})"
    limits = {
        r[0]: int(r[1])
        for r in ch.query(
            "SELECT name, value FROM system.merge_tree_settings "
            "WHERE name IN ('parts_to_delay_insert', 'parts_to_throw_insert')"
        ).result_rows
    }
    peak: dict[str, int] = {}
    for smp in samples:
        for table, v in smp["parts"].items():
            peak[table] = max(peak.get(table, 0), int(v["max_in_partition"]))
    sent = len(lat) + sum(errors.values())
    out = {
        "servers": len(urls),
        "rate": args.rate,
        "max_in_flight": args.max_in_flight,
        "seconds": round(t1 - t0, 1),
        "offered_rate": round(sent / max(1e-9, sent_for), 1),
        "not_sent_at_cap": skipped,
        "served_rate": round(len(lat) / max(1e-9, t1 - t0), 1),
        "writes": _summary(lat, dict(errors)),
        "new_parts_per_s": parts_per_s,
        "max_parts_in_a_partition": peak,
        "max_merges_in_flight": max((smp["merges"] for smp in samples), default=0),
        "samples": len(samples),
        "samples_failed": len(sample_errors),
        "limits": limits,
        "after": _partition_sample(ch),
        "host_load_1m": round(os.getloadavg()[0], 1),
    }
    print(json.dumps(out, indent=1))
    _save(args.out, out)


def optimize(args: argparse.Namespace) -> None:
    """Vector reads against part count: before, then after OPTIMIZE FINAL of the table."""
    env = _env(args.env)
    ch = ch_client(env)
    own_stores(ch)
    rng = random.Random(11)
    tenants = probe_tenants(ch, args.shape)
    out: dict[str, Any] = {"before": [], "after": []}
    for phase in ("before",) if args.before_only else ("before", "after"):
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
    _save(args.out, out)
    print(f"wrote {args.out}")


def erase(args: argparse.Namespace) -> None:
    """Time a user erasure and an org erasure through the server's own ops."""
    env = _env(args.env)
    ch = ch_client(env)
    own_stores(ch)
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
                    # Loom's own stores only: these private stores have no external
                    # backend, so the org saga runs the steps the user saga does.
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
    _save(args.out, out)


def pg_dead_rows(env: dict[str, str], fraction: float) -> dict[str, dict[str, int]]:
    """Rewrite about `fraction` of the harness's heads and revisions in place, leaving
    that many dead tuples for a vacuum to reclaim; the dead and live counts after."""

    async def go(pg: Any) -> dict[str, dict[str, int]]:
        every = max(1, round(1 / fraction))
        await pg.execute(
            "UPDATE loom.memory_current SET version = version "
            "WHERE starts_with(org, 'scale-') AND hashtext(memory_key) % $1 = 0",
            every,
        )
        await pg.execute(
            "UPDATE loom.memory_revisions SET request_id = request_id "
            "WHERE starts_with(org, 'scale-') AND seq % $1 = 0",
            every,
        )
        await pg.execute("SELECT pg_stat_force_next_flush()")
        rows = await pg.fetch(
            "SELECT relname, n_dead_tup, n_live_tup FROM pg_stat_user_tables "
            "WHERE schemaname = 'loom' AND relname IN ('memory_current', 'memory_revisions')"
        )
        return {r["relname"]: {"dead": r["n_dead_tup"], "live": r["n_live_tup"]} for r in rows}

    return _pg_run(env, go)


def pg_ops(args: argparse.Namespace) -> None:
    env = _env(args.env)
    out: dict[str, Any] = {"sizes": pg_sizes(env)}
    if args.dead_fraction:
        out["dead_rows"] = pg_dead_rows(env, args.dead_fraction)
        out["sizes_with_dead_rows"] = pg_sizes(env)  # the heap the vacuum below scans
    out["vacuum_s"] = {t: pg_vacuum_s(env, t) for t in ("memory_current", "memory_revisions")}
    if args.dump_org:
        out["dump_org"] = {"org": args.dump_org, **pg_dump_org_s(env, args.dump_org)}
    print(json.dumps(out, indent=2))
    _save(args.out, out)


def set_authority(args: argparse.Namespace) -> None:
    """Record `--to` as this private deployment's authority, in ClickHouse and in
    Postgres under one deployment id, so a server of either authority boots on the rows
    loaded. Only for a ClickHouse and a Postgres of the harness's own: a deployment's
    authority is recorded once and never switched this way."""
    env = _env(args.env)
    ch = ch_client(env)
    own_stores(ch)
    rows = ch.query("SELECT deployment_id FROM loom.storage_authority FINAL").result_rows
    deployment = rows[0][0] if rows else str(uuid.uuid4())
    if args.to == "postgres":

        async def record(pg: Any) -> None:
            await own_pg_stores(pg)
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
    o.add_argument("--before-only", action="store_true", help="probe only; no OPTIMIZE")
    o.add_argument("--out", required=True)
    e = sub.add_parser("erase")
    e.add_argument("--base-url", required=True)
    e.add_argument("--user", help="org/user_id")
    e.add_argument("--org")
    e.add_argument("--out", required=True)
    g = sub.add_parser("pg")
    g.add_argument("--dump-org")
    g.add_argument(
        "--dead-fraction",
        type=float,
        default=0.0,
        help="rewrite this share of the rows first, so the vacuum has dead rows to reclaim",
    )
    g.add_argument("--out", required=True)
    f = sub.add_parser("fleet", help="memory.set at a fixed rate over several servers")
    f.add_argument("--base-url", required=True, help="comma-separated server URLs")
    f.add_argument("--rate", type=float, required=True, help="writes a second, all servers")
    f.add_argument("--seconds", type=float, default=60)
    f.add_argument("--namespaces", type=int, default=16)
    f.add_argument("--max-in-flight", type=int, default=512)
    f.add_argument("--timeout", type=float, default=60)
    f.add_argument("--out", required=True)
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
        "fleet": fleet,
    }[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
