"""Native operations for the isolated trigger acceptance fixture."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import hashlib
import json
import os
import re
import socket
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from capture import (
    Codec,
    Workload,
    closed,
    equal,
    hex64,
    identifier,
    integer,
    json_object,
    mint_metadata,
    protocol,
    verify_token,
)
from fixture import (
    FILES,
    GIB,
    HERE,
    POLICY,
    PROTOCOL_SHA,
    Refused,
    child_env,
    database,
    digest,
    native_failure_metadata,
    need,
    pg_dsn,
    read,
    ref,
    sha,
    utc,
    write,
)

COUNTS = ("head_global", "head_scope", "revision_global", "revision_scope")
DEBT = (
    "delivery_global",
    "delivery_scope",
    "audit_global",
    "audit_scope",
    "error_global",
    "error_scope",
)
PG_FIELDS = (
    tuple(
        "c_" + k
        for k in (
            "org",
            "namespace",
            "agent",
            "user_id",
            "session_id",
            "memory_key",
            "memory_id",
            "version",
            "seq",
            "content_sha256",
        )
    )
    + ("head",)
    + tuple(
        "r_" + k
        for k in (
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
        )
    )
    + ("revision", "audit", "targets", "attempts", "last_error")
)


def pg_census_sql():
    # One statement snapshot supplies all ten fixed counts and server observations.
    counts = []
    for label, table in (
        ("head", "memory_current"),
        ("revision", "memory_revisions"),
        ("delivery", "memory_deliveries"),
        ("audit", "audit_outbox"),
        ("error", "memory_deliveries"),
    ):
        predicate = {"delivery": "targets<>0", "error": "(attempts<>0 OR last_error<>'')"}.get(
            label, "TRUE"
        )
        limit = 45501 if label in {"head", "revision"} else 1
        for scope in ("global", "scope"):
            where = predicate + (" AND org=$1 AND namespace=$2" if scope == "scope" else "")
            counts.append(
                f"(SELECT count(*) FROM (SELECT 1 FROM loom.{table} WHERE "
                f"{where} LIMIT {limit}) bounded) AS {label}_{scope}"
            )
    return (
        "SELECT "
        + ",".join(counts)
        + (
            ",CASE WHEN octet_length(pg_current_snapshot()::text)<=16384 "
            "THEN pg_current_snapshot()::text ELSE NULL END AS "
            "snapshot,current_setting('transaction_isolation') AS "
            "isolation,current_setting('transaction_read_only') AS "
            "read_only,current_database() AS database,pg_backend_pid() "
            "AS backend_pid"
        )
    )


def pg_record(value, fields):
    need(
        value is not None and list(value.keys()) == list(fields), "closed ordered PG result columns"
    )
    return dict(value)


async def begin_observation(conn, context, isolation, *, backend=None):
    initial = conn.is_in_transaction()
    need(initial is False, "idle PG snapshot boundary")
    statement = "BEGIN ISOLATION LEVEL " + isolation.upper() + " READ ONLY"
    status = await conn.execute(statement)
    active = conn.is_in_transaction()
    need(status == "BEGIN" and active is True, "PG read-only BEGIN acknowledged")
    value = pg_record(
        await conn.fetchrow(pg_census_sql(), context["org"], context["namespace"]),
        (
            *COUNTS[:2],
            *COUNTS[2:],
            *DEBT[:2],
            *DEBT[2:4],
            *DEBT[4:],
            "snapshot",
            "isolation",
            "read_only",
            "database",
            "backend_pid",
        ),
    )
    for key in COUNTS:
        need(integer(value[key]) == 45500, "exact global/scoped PG census")
    for key in DEBT:
        need(integer(value[key], 0, 1) == 0, "settled global/scoped PG debt")
    snapshot = value.pop("snapshot")
    need(
        type(snapshot) is str
        and len(snapshot.encode()) <= 16384
        and re.fullmatch(r"[0-9]+:[0-9]+:(?:[0-9]+(?:,[0-9]+)*)?", snapshot),
        "actual bounded PG snapshot",
    )
    need(
        value["isolation"] == isolation
        and value["read_only"] == "on"
        and value["database"] == context["databases"]["pg_cell"],
        "actual PG snapshot settings/database",
    )
    integer(value["backend_pid"], 1, 2**31 - 1)
    need(backend is None or backend == value["backend_pid"], "same owned PG backend")
    return dict(
        snapshot=snapshot,
        snapshot_bytes=len(snapshot.encode()),
        snapshot_sha256=hashlib.sha256(snapshot.encode()).hexdigest(),
        isolation=value["isolation"],
        read_only=value["read_only"],
        database=value["database"],
        backend_pid=value["backend_pid"],
        begin_status=status,
        initial_transaction_state=initial,
        active_transaction_state=active,
        counts={key: value[key] for key in (*COUNTS, *DEBT)},
    )


async def end_observation(conn, observation):
    status = await conn.execute("COMMIT")
    ended = conn.is_in_transaction()
    need(status == "COMMIT" and ended is False, "PG snapshot end acknowledged")
    observation.update(end_status=status, ended_transaction_state=ended)


async def ch_result(ch, query, parameters, fields, limit):
    result = await ch.query(
        query,
        parameters=parameters,
        settings={
            "max_result_rows": limit,
            "max_result_bytes": 20 * 1024 * 1024,
            "result_overflow_mode": "throw",
            "max_execution_time": 30,
        },
    )
    need(list(result.column_names) == list(fields), "closed ordered CH result columns")
    data = result.result_rows
    need(
        type(data) in {list, tuple}
        and len(data) <= limit
        and all(type(row) in {list, tuple} and len(row) == len(fields) for row in data),
        "bounded CH tuples before maps",
    )
    return data


async def ch_census(ch, context):
    db = database(context["databases"]["ch_schema"])
    observed = {}
    for label, table in (("memory", "memory"), ("audit", "audit_log")):
        for scope in ("global", "scope"):
            where = "event_type='memory.revision'" if label == "audit" else "1"
            if scope == "scope":
                where += " AND org={org:String} AND namespace={ns:String}"
            result = await ch_result(
                ch,
                f"SELECT count() AS n FROM (SELECT 1 FROM `{db}`.{table}"
                + (" FINAL" if label == "memory" else "")
                + f" WHERE {where} LIMIT 45501)",
                {"org": context["org"], "ns": context["namespace"]},
                ("n",),
                1,
            )
            need(
                len(result) == 1 and integer(result[0][0]) == 45500,
                "exact CH global/scoped workload census",
            )
            observed[label + "_" + scope] = result[0][0]
    return observed


def pg_page_sql():
    c = (
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "memory_key",
        "memory_id",
        "version",
        "seq",
        "content_sha256",
    )
    r = (
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
    )
    fields = (
        [f"c.{k} AS c_{k}" for k in c]
        + ["c.row::text AS head"]
        + [f"r.{k} AS r_{k}" for k in r]
        + [
            "r.row::text AS revision",
            "r.audit::text AS audit",
            "d.targets",
            "d.attempts",
            "d.last_error",
        ]
    )
    text = ["c." + k for k in c if k not in {"memory_id", "version", "seq"}] + [
        "r." + k for k in r if k not in {"memory_id", "version", "seq"}
    ]
    sizes = " AND ".join(f"octet_length({k})<=512" for k in text)
    return (
        "SELECT "
        + ",".join(fields)
        + (
            " FROM loom.memory_current c JOIN loom.memory_revisions r ON "
            "(r.org,r.namespace,r.seq)=(c.org,c.namespace,c.seq) LEFT "
            "JOIN loom.memory_deliveries d ON "
            "(d.org,d.namespace,d.seq)=(c.org,c.namespace,c.seq) WHERE "
            "c.org=$1 AND c.namespace=$2 AND c.memory_id=ANY($3::uuid[]) "
            "AND octet_length(c.row::text)<=65536 AND "
            "octet_length(r.row::text)<=65536 AND "
            "octet_length(r.audit::text)<=16384 AND "
        )
        + sizes
        + (
            " AND (d.last_error IS NULL OR "
            "octet_length(d.last_error)<=512) ORDER BY "
            "c.memory_id,c.agent,c.user_id,c.session_id,c.memory_key "
            "LIMIT $4"
        )
    )


def complete_ids(values, expected):
    # Inspect multiplicity BEFORE constructing lookup maps.
    need(
        len(values) == len(expected)
        and len(set(values)) == len(values)
        and set(values) == set(expected),
        "complete unique result multiset before map",
    )


def typed_revision(stored, row, audit, item, seen):
    for prefix, names in (
        (
            "c_",
            (
                "org",
                "namespace",
                "agent",
                "user_id",
                "session_id",
                "memory_key",
                "memory_id",
                "version",
                "content_sha256",
            ),
        ),
        (
            "r_",
            (
                "org",
                "namespace",
                "user_id",
                "memory_id",
                "version",
                "content_sha256",
                "actor",
                "source_class",
            ),
        ),
    ):
        for key in names:
            value = stored[prefix + key]
            if key == "memory_id":
                value = identifier(value)
            elif key == "version":
                value = integer(value, 1, 2**63 - 1)
            else:
                need(type(value) is str and len(value.encode()) <= 512, "typed PG identity text")
            equal(value, row[key])
    for key, expected in (
        ("memory_key", item["key"]),
        ("memory_id", item["memory_id"]),
        ("version", item["version"]),
        ("content_sha256", item["content_sha256"]),
    ):
        equal(row[key], expected)
    seq = integer(stored["c_seq"], 1, 2**63 - 1)
    need(
        integer(stored["r_seq"], 1, 2**63 - 1) == seq and seq not in seen,
        "unique exact PG revision link",
    )
    seen.add(seq)
    need(len(seen) <= 45500, "PG seq bound")
    equal(stored["r_request_id"], audit["request_id"])
    delivery = [stored[key] for key in ("targets", "attempts", "last_error")]
    if delivery != [None, None, None]:
        need(
            integer(delivery[0], 0, 0) == 0
            and integer(delivery[1], 0, 0) == 0
            and type(delivery[2]) is str
            and delivery[2] == "",
            "settled typed PG delivery",
        )
    return seq


def chain_update(chain, index, value):
    raw = json.dumps(
        dict(index=index, value=value), sort_keys=True, allow_nan=False, separators=(",", ":")
    ).encode()
    chain.update(len(raw).to_bytes(8, "big"))
    chain.update(raw)


async def verify_pages(conn, ch, context, workload, codec, native_hash):
    db = database(context["databases"]["ch_schema"])
    seen_seq = set()
    seen_hash = set()
    count = 0
    expected_chain = hashlib.sha256()
    observed_chain = hashlib.sha256()
    authority_chain = hashlib.sha256()
    raw_keys = hashlib.sha256()
    for page in workload.pages():
        need(0 < len(page) <= 100, "fixed proof page")
        ids = [item["memory_id"] for item in page]
        complete_ids(ids, ids)
        fetched = await conn.fetch(
            pg_page_sql(),
            context["org"],
            context["namespace"],
            [uuid.UUID(value) for value in ids],
            len(page) + 1,
        )
        need(len(fetched) == len(page), "PG page exact cardinality/overflow")
        values = [pg_record(row, PG_FIELDS) for row in fetched]
        complete_ids([identifier(row["c_memory_id"]) for row in values], ids)
        heads = {identifier(row["c_memory_id"]): row for row in values}
        checked = {}
        audits = {}
        hashes = []
        for item in page:
            stored = heads[item["memory_id"]]
            head = json_object(stored["head"])
            revision = json_object(stored["revision"])
            row = codec.row(head, origin="pg", comparison=True)
            other = codec.row(revision, origin="pg", comparison=True)
            equal(row, other)
            audit = codec.audit(json_object(stored["audit"]), origin="pg")
            native = dict(audit, ts=datetime.datetime.fromisoformat(audit["ts"]))
            need(native_hash(native), "actual decoded native audit hash")
            seq = typed_revision(stored, row, audit, item, seen_seq)
            codec.expected(
                row,
                audit,
                context,
                api=item["kind"] == "api",
                metadata=workload.metadata,
                driver=workload.driver_header
                or dict(host=socket.gethostname(), pid=workload.driver["pid"]),
            )
            if item["kind"] == "delivery":
                checked_delivery_row = codec.row(item["row"], comparison=True)
                equal(row, checked_delivery_row)
                equal(audit, item["audit"])
            need(audit["row_hash"] not in seen_hash, "unique workload audit identity")
            seen_hash.add(audit["row_hash"])
            hashes.append(audit["row_hash"])
            checked[item["memory_id"]] = row
            audits[audit["row_hash"]] = audit
            intent = {
                key: item[key] for key in ("kind", "key", "memory_id", "version", "content_sha256")
            }
            observed = dict(
                intent,
                key=row["memory_key"],
                memory_id=row["memory_id"],
                version=row["version"],
                content_sha256=row["content_sha256"],
            )
            if item["kind"] == "delivery":
                intent.update(row=checked_delivery_row, audit=item["audit"])
                observed.update(row=row, audit=audit)
            chain_update(expected_chain, count, intent)
            chain_update(observed_chain, count, observed)
            chain_update(authority_chain, count, dict(row=row, audit=audit, seq=seq))
            chain_update(raw_keys, count, dict(head=sorted(head), revision=sorted(revision)))
            count += 1
        fields = codec.p["memory_fields"]
        data = await ch_result(
            ch,
            "SELECT "
            + ",".join("`" + key + "`" for key in fields)
            + (
                f" FROM `{db}`.memory FINAL WHERE org={{org:String}} AND "
                f"namespace={{ns:String}} AND memory_id IN "
                f"{{ids:Array(UUID)}} ORDER BY "
                f"memory_id,agent,user_id,session_id,memory_key LIMIT "
                f"{len(page) + 1}"
            ),
            dict(
                org=context["org"], ns=context["namespace"], ids=[uuid.UUID(value) for value in ids]
            ),
            fields,
            len(page) + 1,
        )
        complete_ids([identifier(row[fields.index("memory_id")]) for row in data], ids)
        for value in data:
            row = codec.row(dict(zip(fields, value, strict=True)), origin="ch", comparison=True)
            equal(row, checked[row["memory_id"]])
        fields = codec.p["audit_fields"]
        data = await ch_result(
            ch,
            "SELECT "
            + ",".join("`" + key + "`" for key in fields)
            + (
                f" FROM `{db}`.audit_log WHERE org={{org:String}} AND "
                f"namespace={{ns:String}} AND event_type='memory.revision' "
                f"AND row_hash IN {{hashes:Array(String)}} ORDER BY row_hash "
                f"LIMIT {len(page) + 1}"
            ),
            dict(org=context["org"], ns=context["namespace"], hashes=hashes),
            fields,
            len(page) + 1,
        )
        complete_ids([hex64(row[fields.index("row_hash")], native=True) for row in data], hashes)
        for value in data:
            audit = codec.audit(dict(zip(fields, value, strict=True)), origin="ch")
            equal(audit, audits[audit["row_hash"]])
    need(count == len(seen_seq) == len(seen_hash) == 45500, "complete exact workload identities")
    need(
        expected_chain.hexdigest() == observed_chain.hexdigest(),
        "streamed intended/observed proof chain",
    )
    return dict(
        rows=count,
        api=workload.counts["api"],
        delivery=workload.counts["delivery"],
        expected_sha256=expected_chain.hexdigest(),
        observed_sha256=observed_chain.hexdigest(),
        authority_sha256=authority_chain.hexdigest(),
        pg_raw_key_sets_sha256=raw_keys.hexdigest(),
    )


def codec_contract():
    from server.memory.store.pg_memory import DATETIME_COLS
    from server.memory.store.rows import FIRST_VERSION_DEFAULTS, MEMORY_PERSISTED_COLS

    specification = protocol()
    codec = Codec(specification)
    equal(list(MEMORY_PERSISTED_COLS), specification["memory_fields"])
    equal(FIRST_VERSION_DEFAULTS, specification["sparse_defaults"])
    equal(
        sorted(DATETIME_COLS),
        sorted(
            key
            for key, value in specification["memory_types"].items()
            if value.endswith("utc_second")
        ),
    )
    return codec


async def independent_provenance(context, output):
    from server.observability.audit import row_matches_hash

    codec = codec_contract()
    context = dict(
        context, _api_server=read(Path(context["workdir"]) / "serving-cell/launch.json")["server"]
    )
    first = None
    summary = None
    try:
        with Workload(context, codec) as workload:
            async with connections(context) as (conn, ch):
                try:
                    page = await begin_observation(conn, context, "repeatable read")
                    initial_ch = await ch_census(ch, context)
                    checked = await verify_pages(
                        conn, ch, context, workload, codec, row_matches_hash
                    )
                    await end_observation(conn, page)
                    final = await begin_observation(
                        conn, context, "read committed", backend=page["backend_pid"]
                    )
                    await end_observation(conn, final)
                    need(
                        conn.is_in_transaction() is False,
                        "final CH checks outside both PG snapshots",
                    )
                    final_ch = await ch_census(ch, context)
                    summary = dict(
                        schema=2,
                        fixture_id=context["fixture_id"],
                        verified=True,
                        provenance=True,
                        **checked,
                        pg_page_observation=page,
                        pg_final_observation=final,
                        ch_initial_census=initial_ch,
                        ch_final_census=final_ch,
                        inputs=workload.refs,
                        source=dict(
                            head=context["head"],
                            tree=context["tree"],
                            hashes=context["source_hashes"],
                        ),
                        driver_birth=workload.driver,
                        api_birth=context["_api_server"],
                        mint_metadata=context["mint_metadata"],
                        hash_and_independent_stamps=True,
                        utc=utc(),
                    )
                except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                    first = error
                finally:
                    try:
                        if conn.is_in_transaction():
                            await conn.execute("ROLLBACK")
                    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                        first = error if first is None else first
                if first is not None:
                    raise first
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        first = error if first is None else first
    if first is not None:
        raise first
    need(summary is not None, "complete proof before closure")
    write(output, summary)


def api(context: dict, output: Path) -> None:
    import uvicorn

    from bench.cell_trigger_gate import _workload
    from server.core import deployment_config, instances
    from server.core.provider_process import _start_ticks
    from server.settings import settings

    actual = settings.model_dump(mode="json")
    need(
        actual["router_operator_token"] == ""
        and re.fullmatch(r"lcr_[0-9a-f]{128}", actual["cell_router_token"]),
        "API credential posture",
    )
    candidate = dict(actual, cell_router_token="")
    common = {k: v for k, v in actual.items() if k != "cell_router_token"}
    folder = output.parent
    artifact = write(
        folder / "configuration.json",
        {
            "schema": 2,
            "api_configuration_sha256": digest(actual),
            "cli_configuration_sha256": digest(candidate),
            "shared_configuration_sha256": digest(common),
            "workload": _workload(settings),
            "fingerprints": deployment_config.fingerprints(settings),
            "credential_posture": {
                "api_cell_router_token_present": True,
                "cli_cell_router_token_present": False,
                "api_router_operator_token_present": False,
                "cli_router_operator_token_present": False,
            },
        },
    )
    write(folder / "private-settings.json", actual, limit=1024 * 1024)
    write(
        output,
        {
            "schema": 2,
            "server": {
                "pid": os.getpid(),
                "start_ticks": _start_ticks(os.getpid()),
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "uid": os.geteuid(),
                "cwd": os.getcwd(),
                "health_instance": f"{socket.gethostname()}:{os.getpid()}",
                "instance_id": instances.instance_id(),
            },
            "source": {"head": context["head"], "tree": context["tree"]},
            "configuration_artifact": artifact,
        },
    )
    from contextlib import contextmanager
    from contextvars import ContextVar

    from server.core.ch import current_query_tag
    from server.observability import otel

    # These ordinals count POST memory.set ASGI calls, not successful writes.
    samples = (8192, 10240, 12288, 14336, 16384, 18432)
    stages = {
        "entity.resolve": 1,
        "pg.commit.memory": 2,
        "pg.publish": 3,
        "embed.compute": 4,
        "conflicts.check_on_commit": 5,
        "memory.set.post_insert": 6,
        "ch.query.existing_memory": 7,
        "ch.query.type_config": 8,
        "blacklist.enforce": 9,
    }
    sample_var = ContextVar("native_request_cost_sample", default=None)
    ordinal = emitted = dropped = byte_count = 0
    disabled = False
    stream = None
    origin = time.perf_counter_ns()
    try:
        stream = (folder / "request-costs.jsonl").open("x", encoding="ascii")
    except OSError:
        disabled = True

    def emit_cost(row):
        nonlocal emitted, dropped, byte_count, disabled
        if disabled:
            return
        data = json.dumps(row, separators=(",", ":")) + "\n"
        if (
            len(data) > 512
            or byte_count + len(data) > 65536
            or (row[0] in (1, 2) and emitted >= 192)
        ):
            dropped += 1
            return
        try:
            stream.write(data)
            stream.flush()
            byte_count += len(data)
            emitted += int(row[0] in (1, 2))
        except (OSError, ValueError):
            disabled = True

    emit_cost([0, 1, *samples])
    original_traced = otel.traced

    def measured_traced(name, **attrs):
        sample = sample_var.get()
        stage = stages.get(name)
        if sample is None or stage is None:
            return original_traced(name, **attrs)

        @contextmanager
        def measured_span():
            started, cpu = time.perf_counter_ns(), time.process_time_ns()
            # Only a digest of the actual transport-bound tag leaves this process.
            tag = current_query_tag()
            tag_hash = int.from_bytes(hashlib.sha256(tag.encode()).digest()) if tag else 0
            try:
                with original_traced(name, **attrs) as span:
                    yield span
            finally:
                emit_cost(
                    [
                        1,
                        sample["ordinal"],
                        stage,
                        started - origin,
                        time.perf_counter_ns() - started,
                        time.process_time_ns() - cpu,
                        tag_hash,
                        0,
                        int(sys.exc_info()[0] is not None),
                        sample["done"],
                        0,
                    ]
                )

        return measured_span()

    def measured_application():
        # Uvicorn still imports the normal app on its running loop, before lifespan.
        from server.main import app

        async def measured(scope, receive, send):
            nonlocal ordinal
            if scope.get("type") != "http":
                return await app(scope, receive, send)
            if scope.get("method") == "POST" and scope.get("path") == "/v1/memory.set":
                ordinal += 1
                selected = ordinal in samples
            else:
                selected = False
            sample = {"ordinal": ordinal, "done": 0} if selected else None
            token = sample_var.set(sample)
            status = tag_hash = body_finished = 0
            started, cpu = time.perf_counter_ns(), time.process_time_ns()

            async def measured_send(message):
                nonlocal status, tag_hash, body_finished
                await send(message)
                # Observation follows the successful send and never changes a message.
                try:
                    if message["type"] == "http.response.start":
                        status = int(message["status"])
                        for key, value in message.get("headers", ()):
                            if key.lower() == b"x-request-id" and len(value) <= 64:
                                tag_hash = int.from_bytes(
                                    hashlib.sha256(b"memory.set " + value).digest()
                                )
                    elif message["type"] == "http.response.body" and not message.get("more_body"):
                        body_finished = 1
                except (AttributeError, TypeError, ValueError):
                    tag_hash = 0

            try:
                return await app(scope, receive, measured_send if selected else send)
            finally:
                if sample is not None:
                    sample["done"] = 1
                    emit_cost(
                        [
                            2,
                            sample["ordinal"],
                            0,
                            started - origin,
                            time.perf_counter_ns() - started,
                            time.process_time_ns() - cpu,
                            tag_hash,
                            status,
                            int(sys.exc_info()[0] is not None),
                            1,
                            body_finished,
                        ]
                    )
                sample_var.reset(token)

        return measured

    otel.traced = measured_traced
    try:
        uvicorn.run(
            measured_application,
            factory=True,
            app_dir=context["root"],
            host="127.0.0.1",
            port=context["ports"]["api"],
            loop="asyncio",
            workers=1,
            reload=False,
        )
    finally:
        otel.traced = original_traced
        emit_cost([3, ordinal, emitted, dropped, int(disabled)])
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


@asynccontextmanager
async def connections(context: dict, *, admin: bool = False):
    import asyncpg
    import clickhouse_connect

    from server.core import pg

    conn = ch = None
    first = None
    try:
        dsn = (
            context["pg_admin_dsn"]
            if admin
            else pg_dsn(context["pg_admin_dsn"], context["databases"]["pg_cell"])
        )
        conn = await asyncpg.connect(
            dsn,
            timeout=10,
            command_timeout=30,
            server_settings={
                "application_name": "trigger-fixture:" + context["fixture_id"],
                "statement_timeout": "30000",
                "idle_in_transaction_session_timeout": "30000",
            },
        )
        values = {k: context["ch"][k] for k in ("host", "port", "username", "password")}
        ch = await clickhouse_connect.get_async_client(
            **values,
            database="",
            query_retries=0,
            connect_timeout=10,
            send_receive_timeout=30,
            settings={"log_comment": "fixture.private", "async_insert": 0},
        )
        yield conn, ch
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        first = error
    finally:
        if ch is not None:
            try:
                async with asyncio.timeout(15):
                    await ch.close()
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                first = error if first is None else first
        if conn is not None:
            try:
                await pg.close_owned(conn, end=time.monotonic() + 15)
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                first = error if first is None else first
    if first is not None:
        raise first


async def rows(ch, query: str, parameters: dict | None = None) -> list[dict]:
    result = await ch.query(query, parameters=parameters or {})
    return list(result.named_results())


async def stores(context: dict, role: str, output: Path, *, drop: bool = False) -> None:
    spec = context
    db = database(spec["databases"][role])
    pg_role = role.startswith("pg_")
    async with connections(spec, admin=True) as (conn, ch):
        if drop:
            creation = read(Path(spec["workdir"]) / (role + "-created.json"))
            need(
                creation["database"] == db and creation["fixture_id"] == spec["fixture_id"],
                "exact created database",
            )
            expected_id = spec["stores"]["pg_primary" if pg_role else "clickhouse"]["container_id"]
            need(creation["store_container_id"] == expected_id, "created store identity")
            if pg_role:
                need(
                    await conn.fetchval(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=$1", db
                    )
                    == 0,
                    "scratch PG sessions",
                )
                exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT FROM pg_database WHERE datname=$1)", db
                )
                if exists:
                    need(
                        str(await conn.fetchval("SELECT oid FROM pg_database WHERE datname=$1", db))
                        == creation["database_id"],
                        "same created PG database",
                    )
                    await conn.execute(f'DROP DATABASE "{db}"')
            else:
                # Refuse asynchronous or running writes; do not flush a global queue.
                aliases = [spec["databases"]["ch_schema"], spec["databases"]["ch_telemetry"]]
                need(
                    (
                        await rows(
                            ch,
                            (
                                "SELECT count() AS n FROM system.asynchronous_inserts WHERE "
                                "database IN {dbs:Array(String)}"
                            ),
                            {"dbs": aliases},
                        )
                    )
                    == [{"n": 0}],
                    "CH async writers",
                )
                live = await rows(
                    ch,
                    (
                        "SELECT count() AS n FROM system.processes WHERE query_id != "
                        "currentQueryID() AND (current_database IN "
                        "{dbs:Array(String)} OR arrayExists(d -> position(query, d) "
                        "> 0, {dbs:Array(String)}))"
                    ),
                    {"dbs": aliases},
                )
                need(live == [{"n": 0}], "CH running writers")
                current = await rows(
                    ch,
                    "SELECT toString(uuid) AS id FROM system.databases WHERE name={db:String}",
                    {"db": db},
                )
                need(
                    not current or current == [{"id": creation["database_id"]}],
                    "same created CH database",
                )
                await ch.command(f"DROP DATABASE IF EXISTS `{db}` SYNC")
            write(
                output,
                {
                    "schema": 2,
                    "fixture_id": spec["fixture_id"],
                    "database": db,
                    "dropped": True,
                    "utc": utc(),
                },
            )
            return
        need(await conn.fetchval("SELECT NOT pg_is_in_recovery()"), "PG primary")
        need(
            160000 <= int(await conn.fetchval("SHOW server_version_num")) < 170000,
            "owned PG16 primary",
        )
        need(await conn.fetchval("SHOW max_prepared_transactions") == "0", "prepared transactions")
        need(await conn.fetchval("SHOW synchronous_standby_names"), "synchronous standby names")
        need(
            await conn.fetchval(
                "SELECT EXISTS(SELECT FROM pg_stat_replication WHERE "
                "state='streaming' AND sync_state='sync' AND flush_lsn IS "
                "NOT NULL)"
            ),
            "synchronous standby",
        )
        need(
            await conn.fetchval("SELECT pg_size_bytes(current_setting('shared_buffers'))")
            == spec["stores"]["pg_primary"]["shared_buffers_bytes"],
            "primary buffers",
        )
        import asyncpg

        standby = await asyncpg.connect(
            spec["pg_standby_dsn"],
            timeout=10,
            command_timeout=10,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": "10000",
                "application_name": "trigger-fixture:" + context["fixture_id"],
            },
        )
        standby_error = None
        try:
            need(await standby.fetchval("SELECT pg_is_in_recovery()"), "standby recovery")
            need(
                160000 <= int(await standby.fetchval("SHOW server_version_num")) < 170000,
                "owned PG16 standby",
            )
            need(
                await standby.fetchval("SELECT pg_size_bytes(current_setting('shared_buffers'))")
                == spec["stores"]["pg_standby"]["shared_buffers_bytes"],
                "standby buffers",
            )
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            standby_error = error
        try:
            from server.core import pg

            await pg.close_owned(standby, end=time.monotonic() + 15)
        except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            standby_error = error if standby_error is None else standby_error
        if standby_error is not None:
            raise standby_error
        limits = await rows(
            ch, "SELECT value FROM system.server_settings WHERE name='max_server_memory_usage'"
        )
        need(
            len(limits) == 1
            and int(limits[0]["value"])
            == spec["stores"]["clickhouse"]["server_limit_bytes"]
            == 9 * GIB // 2,
            "CH server memory cap",
        )
        if pg_role:
            need(
                not await conn.fetchval(
                    "SELECT EXISTS(SELECT FROM pg_database WHERE datname=$1)", db
                ),
                "new PG database",
            )
            write(
                Path(spec["workdir"]) / (role + "-intent.json"),
                {
                    "schema": 2,
                    "fixture_id": spec["fixture_id"],
                    "database": db,
                    "absent": True,
                    "utc": utc(),
                },
            )
            await conn.execute(f"CREATE DATABASE \"{db}\" ENCODING 'UTF8' TEMPLATE template0")
            need(
                await conn.fetchval(
                    "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname=$1", db
                )
                == "UTF8",
                "created UTF8",
            )
        else:
            need(
                await rows(
                    ch,
                    "SELECT count() AS n FROM system.databases WHERE name={db:String}",
                    {"db": db},
                )
                == [{"n": 0}],
                "new CH database",
            )
            existing = await rows(
                ch,
                "SELECT name FROM system.zookeeper WHERE path={path:String}",
                {"path": spec["ch"]["keeper_prefix"]},
            )
            need(
                spec["databases"]["ch_schema"] not in {r["name"] for r in existing},
                "new private Keeper root",
            )
            write(
                Path(spec["workdir"]) / (role + "-intent.json"),
                {
                    "schema": 2,
                    "fixture_id": spec["fixture_id"],
                    "database": db,
                    "absent": True,
                    "keeper_root_absent": True,
                    "utc": utc(),
                },
            )
            await ch.command(f"CREATE DATABASE `{db}` ENGINE=Atomic")
        identity = (
            str(await conn.fetchval("SELECT oid FROM pg_database WHERE datname=$1", db))
            if pg_role
            else (
                await rows(
                    ch,
                    "SELECT toString(uuid) AS id FROM system.databases WHERE name={db:String}",
                    {"db": db},
                )
            )[0]["id"]
        )
        write(
            output,
            {
                "schema": 2,
                "fixture_id": spec["fixture_id"],
                "role": role,
                "store_container_id": spec["stores"]["pg_primary" if pg_role else "clickhouse"][
                    "container_id"
                ],
                "database": db,
                "database_id": identity,
                "created_at": utc(),
                "created": True,
            },
        )


async def resolve_creation(context: dict, role: str, output: Path) -> None:
    name = database(context["databases"][role])
    async with connections(context, admin=True) as (conn, ch):
        if role.startswith("pg_"):
            present = bool(
                await conn.fetchval("SELECT EXISTS(SELECT FROM pg_database WHERE datname=$1)", name)
            )
        else:
            present = (
                await rows(
                    ch,
                    "SELECT count() AS n FROM system.databases WHERE name={db:String}",
                    {"db": name},
                )
            ) != [{"n": 0}]
        # Presence after an unacknowledged create is not adoption authority.
        write(
            output,
            {
                "schema": 2,
                "fixture_id": context["fixture_id"],
                "role": role,
                "database": name,
                "present": present,
                "utc": utc(),
            },
        )


def mint_retirement(group: list[dict], birth: dict, certified) -> dict:
    matching = []
    for row in group:
        parts = str(row["host_proof"]).split("|")
        if len(parts) == 9 and (parts[0], parts[1], parts[4], parts[5]) == (
            socket.gethostname(),
            birth["boot_id"],
            str(birth["pid"]),
            str(birth["start_ticks"]),
        ):
            matching.append(row)
    need(
        len(matching) == 1
        and matching[0]["state"] == "retired"
        and certified(matching[0]["host_proof"]),
        "mint native positive retirement",
    )
    return {
        "participant_id": str(matching[0]["participant_id"]),
        "state": "retired",
        "version": int(matching[0]["version"]),
        "birth": birth,
    }


async def final_retirement(context: dict, output: Path) -> None:
    from server.core.provider_process import has_certificate

    targets = read(Path(context["workdir"]) / "final-participant-births.json")["processes"]
    need(0 < len(targets) <= 16, "final participant target bound")
    db = database(context["databases"]["ch_schema"])
    async with connections(context) as (_, ch):
        group = await rows(
            ch,
            (
                f"SELECT participant_id,state,version,host_proof FROM "
                f"`{db}`._provider_participants FINAL LIMIT 65"
            ),
        )
        need(len(group) <= 64, "final participant census bound")
        observed = []
        for target in targets:
            birth = ref(target["birth"])
            terminal = ref(target["terminal"])
            need(terminal["returncode"] == target["returncode"], "final terminal receipt")
            observed.append(
                dict(
                    role=target["role"],
                    terminal=target["terminal"],
                    **mint_retirement(group, birth, has_certificate),
                )
            )
        write(
            output,
            {
                "schema": 2,
                "fixture_id": context["fixture_id"],
                "retired": observed,
                "verified": True,
                "utc": utc(),
            },
        )


async def mint_proof(context: dict, output: Path) -> None:
    from server.core.provider_process import has_certificate

    birth = ref(read(Path(context["workdir"]) / "mint-process.json")["launch_receipt"])
    db = database(context["databases"]["ch_schema"])
    async with connections(context) as (_, ch):
        group = await rows(
            ch,
            (
                f"SELECT participant_id,state,version,host_proof FROM "
                f"`{db}`._provider_participants FINAL LIMIT 65"
            ),
        )
        need(len(group) <= 64, "bounded native participant census")
        observed = mint_retirement(group, birth, has_certificate)
        write(output, {"schema": 2, "fixture_id": context["fixture_id"], **observed, "utc": utc()})


async def proof(
    context: dict,
    output: Path,
    *,
    bootstrap: bool = False,
    quiescent: bool = False,
    keeper: bool = False,
) -> None:
    from server.cli.cell_attach_beats import widened_window_s
    from server.core import cell, cell_triggers
    from server.settings import settings

    db = database(context["databases"]["ch_schema"])
    org, ns = context["org"], context["namespace"]
    async with connections(context, admin=quiescent or keeper) as (conn, ch):
        if keeper:
            original = read(Path(context["workdir"]) / "cleanup-keeper-paths.json")
            prefix = context["ch"]["keeper_prefix"].rstrip("/")
            for entry in original["keeper_paths"]:
                need(entry["path"].startswith(prefix + "/"), "recorded Keeper prefix")
                parent = prefix
                absent = False
                for segment in entry["path"][len(prefix) + 1 :].split("/"):
                    children = await rows(
                        ch,
                        "SELECT name FROM system.zookeeper WHERE path={path:String}",
                        {"path": parent},
                    )
                    if segment not in {r["name"] for r in children}:
                        absent = True
                        break
                    parent += "/" + segment
                need(absent, "owned Keeper path remains")
            write(
                output,
                {
                    "schema": 2,
                    "fixture_id": context["fixture_id"],
                    "keeper_paths_absent": True,
                    "utc": utc(),
                },
            )
            return
        if quiescent:
            names = [context["databases"]["pg_cell"], context["databases"]["pg_directory"]]
            need(
                await conn.fetchval(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=ANY($1::text[])", names
                )
                == 0,
                "all scratch PG sessions closed",
            )
            need(
                await conn.fetchval(
                    (
                        "SELECT count(*) FROM pg_stat_activity WHERE "
                        "pid<>pg_backend_pid() AND application_name=$1"
                    ),
                    "trigger-fixture:" + context["fixture_id"],
                )
                == 0,
                "cut-off fixture admin sessions",
            )
            aliases = [db, context["databases"]["ch_telemetry"]]
            need(
                await rows(
                    ch,
                    (
                        "SELECT count() AS n FROM system.asynchronous_inserts WHERE "
                        "database IN {dbs:Array(String)}"
                    ),
                    {"dbs": aliases},
                )
                == [{"n": 0}],
                "quiescent CH queue",
            )
            need(
                await rows(
                    ch,
                    (
                        "SELECT count() AS n FROM system.processes WHERE query_id != "
                        "currentQueryID() AND (current_database IN "
                        "{dbs:Array(String)} OR arrayExists(d -> position(query, d) "
                        "> 0, {dbs:Array(String)}))"
                    ),
                    {"dbs": aliases},
                )
                == [{"n": 0}],
                "quiescent CH writers",
            )
            keeper_paths = []
            for row in await rows(
                ch,
                (
                    "SELECT database,name,create_table_query FROM system.tables "
                    "WHERE database IN {dbs:Array(String)} AND engine='KeeperMap'"
                ),
                {"dbs": aliases},
            ):
                match = re.search(r"KeeperMap\('([^']+)'", row["create_table_query"])
                need(match and match[1].startswith("/" + db + "/"), "quiescent private Keeper path")
                keeper_paths.append(
                    {
                        "database": row["database"],
                        "table": row["name"],
                        "path": context["ch"]["keeper_prefix"].rstrip("/") + match[1],
                    }
                )
            need(len(keeper_paths) <= 64, "Keeper census bound")
            write(
                output,
                {
                    "schema": 2,
                    "fixture_id": context["fixture_id"],
                    "pg_sessions": 0,
                    "ch_writers": 0,
                    "keeper_paths": keeper_paths,
                    "utc": utc(),
                },
            )
            return
        authority = dict(
            await conn.fetchrow("SELECT authority,deployment_id,epoch FROM loom.authority")
        )
        authority["deployment_id"] = str(authority["deployment_id"])
        if bootstrap:
            need(
                authority["authority"] == "postgres" and authority["epoch"] > 0,
                "native boot authority",
            )
            pair = await rows(
                ch, f"SELECT authority,deployment_id,epoch FROM `{db}`.storage_authority FINAL"
            )
            need(
                len(pair) == 1 and all(str(pair[0][k]) == str(v) for k, v in authority.items()),
                "native boot pair",
            )
            params = {"org": org, "ns": ns}
            need(
                await rows(
                    ch,
                    (
                        "SELECT owner_email,default_conflict_policy "
                        f"FROM `{db}`.namespaces FINAL WHERE "
                        f"org={{org:String}} AND namespace={{ns:String}}"
                    ),
                    params,
                )
                == [{"owner_email": "benchmark", "default_conflict_policy": "append_only"}],
                "native registered namespace",
            )
            need(
                await rows(
                    ch,
                    (
                        f"SELECT subject,role FROM `{db}`.namespace_grants FINAL "
                        f"WHERE org={{org:String}} AND namespace={{ns:String}} AND "
                        f"subject='benchmark' AND revoked_at IS NULL"
                    ),
                    params,
                )
                == [{"subject": "benchmark", "role": "admin"}],
                "native registration grant",
            )
            need(
                await conn.fetchval("SELECT count(*) FROM loom.memory_current") == 0,
                "no bootstrap memories",
            )
            write(
                output,
                {
                    "schema": 2,
                    "fixture_id": context["fixture_id"],
                    **authority,
                    "namespace_registered": True,
                    "admin_grant": True,
                    "memory_rows": 0,
                    "utc": utc(),
                },
            )
            return
        first = await conn.fetchrow("SELECT is_first,cell FROM loom.cell_first")
        need(
            first and first["is_first"] is True and first["cell"] == context["cell"],
            "first cell tuple",
        )
        need(await conn.fetchval("SELECT mode FROM loom.tenancy_mode") == "enforced", "enforced")
        need(
            await conn.fetchval("SELECT EXISTS(SELECT FROM loom.tenancy WHERE org=$1)", org),
            "held fixture org",
        )
        own = await conn.fetchrow("SELECT current_fp,next_fp FROM loom.cell_self")
        accepted = (
            json.loads(own["current_fp"])
            if isinstance(own["current_fp"], str)
            else own["current_fp"]
        )
        need(
            own["next_fp"] is None
            and cell.matches(cell.fingerprint(str(authority["deployment_id"])), accepted),
            "accepted stable fingerprint",
        )
        need(not await cell_triggers.missing(conn), "canonical triggers")
        paired = await rows(
            ch, f"SELECT authority,deployment_id,epoch FROM `{db}`.storage_authority FINAL"
        )
        need(
            len(paired) == 1 and all(str(paired[0][k]) == str(v) for k, v in authority.items()),
            "paired authority",
        )
        for table in ("provider_configuration", "runtime_overrides"):
            need(
                await rows(ch, f"SELECT count() AS n FROM `{db}`.{table} FINAL") == [{"n": 0}],
                "empty saved configuration",
            )
        need(
            await conn.fetchval(
                "SELECT (SELECT count(*) FROM loom.memory_deliveries WHERE "
                "targets<>0)+(SELECT count(*) FROM loom.audit_outbox)"
            )
            == 0,
            "settled setup",
        )
        instance = context["api_instance_id"]
        from types import SimpleNamespace

        from server.core import deployment_config, instances
        from server.core.ch_names import NamedClient

        named = NamedClient(ch)
        adapter = SimpleNamespace(query_all=lambda sql, params=None: rows(named, sql, params))
        live = await instances.read_live(adapter, all_deployments=True)
        need(
            len(live) == 1
            and live[0]["instance_id"] == instance
            and live[0]["serving"] == 1
            and live[0]["build_commit"] == context["head"]
            and live[0]["host"] == socket.gethostname()
            and not live[0]["workers_lease"],
            "single serving API source",
        )
        need(
            all(live[0].get(k) == v for k, v in deployment_config.fingerprints(settings).items()),
            "live API fingerprints",
        )
        from server.core.provider_process import has_certificate

        launch = read(Path(context["workdir"]) / "serving-cell/launch.json")["server"]
        participants = await rows(
            ch,
            (
                f"SELECT participant_id,state,host_proof FROM "
                f"`{db}`._provider_participants FINAL LIMIT 65"
            ),
        )
        need(0 < len(participants) <= 64, "bounded native participant evidence")
        serving = 0
        for participant in participants:
            participant["participant_id"] = str(participant["participant_id"])
            proof_value = participant["host_proof"]
            need(has_certificate(proof_value), "native participant capability")
            parts = proof_value.split("|")
            own = (parts[0], parts[1], parts[4], parts[5]) == (
                socket.gethostname(),
                launch["boot_id"],
                str(launch["pid"]),
                str(launch["start_ticks"]),
            )
            if own:
                need(participant["state"] == "running", "serving native participant")
                serving += 1
            else:
                need(
                    participant["state"] == "retired",
                    "prior native participant not positively retired",
                )
        need(serving == 1, "single native serving participant")
        beat = await rows(
            ch,
            (
                f"SELECT fingerprint,cell,tenancy_mode FROM "
                f"`{db}`.cell_heartbeats WHERE instance_id={{id:String}}"
            ),
            {"id": instance},
        )
        need(len(beat) == 1, "API heartbeat")
        value = json.loads(beat[0]["fingerprint"])
        need(
            value.get("serves") is True
            and value.get("marked") is True
            and value.get("router") is True,
            "native API router state",
        )
        namespace = await rows(
            ch,
            (
                f"SELECT owner_email FROM `{db}`.namespaces FINAL WHERE "
                f"org={{org:String}} AND namespace={{ns:String}}"
            ),
            {"org": org, "ns": ns},
        )
        need(namespace == [{"owner_email": "benchmark"}], "native namespace owner")
        keeper = []
        for row in await rows(
            ch,
            (
                "SELECT database,name,create_table_query FROM system.tables "
                "WHERE database IN {dbs:Array(String)} AND engine='KeeperMap'"
            ),
            {"dbs": [db, context["databases"]["ch_telemetry"]]},
        ):
            match = re.search(r"KeeperMap\('([^']+)'", row["create_table_query"])
            need(match and match[1].startswith("/" + db + "/"), "private actual Keeper path")
            keeper.append(
                {
                    "database": row["database"],
                    "table": row["name"],
                    "path": context["ch"]["keeper_prefix"].rstrip("/") + match[1],
                }
            )
        write(
            output,
            {
                "schema": 2,
                "fixture_id": context["fixture_id"],
                **authority,
                "first": [True, context["cell"]],
                "mode": "enforced",
                "held": True,
                "keeper_paths": keeper,
                "native_participants": participants,
                "widened_window_s": widened_window_s(),
                "utc": utc(),
            },
        )


class Fixture:
    def __init__(self, spec: dict, owner) -> None:
        self.s, self.o = dict(spec), owner
        self.w = Path(spec["workdir"])
        self.receipts = []
        self.created = []
        self.teardown_incomplete = False
        self.retirement_attempted = False
        self.retirement_receipt = None
        self.settings: dict = {}
        self.api = None
        self.cell = "gate-" + spec["fixture_id"][:12]
        self.org, self.namespace = (
            "trigger-" + spec["fixture_id"],
            "trigger-" + spec["fixture_id"][:12],
        )
        self.s.update(cell=self.cell, org=self.org, namespace=self.namespace)
        self.context = self.w / "context.json"
        write(self.context, self.s)

    def env(self, settings: dict | None = None, *, ca: bool = False) -> dict:
        return child_env(self.s, self.settings if settings is None else settings, ca=ca)

    def cli(self, *args: str, settings: dict | None = None, seconds: float = 120):
        return self.o.command(
            args[0],
            [self.s["python"], "-m", "server.cli", *args],
            self.env(settings, ca=True),
            seconds=seconds,
        )

    def receipt(self, operation: str, value: dict) -> None:
        reference = write(
            self.w / (operation + "-receipt.json"), {"schema": 2, "operation": operation, **value}
        )
        self.receipts.append({"operation": operation, "receipt": reference})

    def command_receipt(self, operation: str, item: dict, **observations) -> None:
        self.receipt(
            operation,
            {
                "returncode": item["process"].returncode,
                **observations,
                "process": self.process_receipt(item),
                "log": {
                    "path": str(item["log"]),
                    "sha256": sha(item["log"]),
                    "bytes": item["log"].stat().st_size,
                },
                "utc": utc(),
            },
        )

    def action(
        self, task: str, *, role: str = "", settings: dict | None = None, cleanup: bool = False
    ) -> dict:
        target = self.w / (
            role + "-created.json"
            if task == "create"
            else task + ("-" + role if role else "") + ".json"
        )
        if task == "quiescent":
            target = self.w / ("quiescent-" + str(self.o.serial + 1) + ".json")
        argv = [
            self.s["python"],
            str(HERE / FILES[1]),
            "--task",
            task,
            "--context",
            str(self.context),
            "--output",
            str(target),
        ]
        if role:
            argv += ["--role", role]
        try:
            self.o.command(
                task,
                argv,
                self.env(settings),
                seconds=600 if task == "provenance" else 120,
                cleanup=cleanup,
            )
        except BaseException as error:
            if isinstance(error, Refused):
                try:
                    failure = read(target.with_name(target.name + ".failure.json"), limit=4096)
                    closed(
                        failure,
                        "error_type site outcome driver"
                        if "driver" in failure
                        else "error_type site outcome",
                    )
                    if "driver" in failure:
                        driver = failure["driver"]
                        closed(
                            driver,
                            "code causes"
                            + (" sqlstate" if "sqlstate" in driver else "")
                            + (" outcome_unknown" if "outcome_unknown" in driver else ""),
                        )
                        need(
                            (
                                "sqlstate" not in driver
                                or type(driver["sqlstate"]) is str
                                and re.fullmatch(r"[0-9A-Z]{5}", driver["sqlstate"])
                            )
                            and (
                                "outcome_unknown" not in driver
                                or type(driver["outcome_unknown"]) is bool
                            ),
                            "native Postgres failure metadata",
                        )
                        need(
                            (
                                driver["code"] is None
                                or type(driver["code"]) is int
                                and 0 <= driver["code"] < 2**31
                            )
                            and type(driver["causes"]) is list
                            and len(driver["causes"]) <= 4,
                            "native driver failure metadata",
                        )
                        for cause in driver["causes"]:
                            closed(
                                cause,
                                "error_type errno"
                                + (" sqlstate" if "sqlstate" in cause else "")
                                + (" cancel_reason" if "cancel_reason" in cause else "")
                                + (" site" if "site" in cause else ""),
                            )
                            need(
                                "sqlstate" not in cause
                                or type(cause["sqlstate"]) is str
                                and re.fullmatch(r"[0-9A-Z]{5}", cause["sqlstate"]),
                                "native Postgres failure cause",
                            )
                            if "cancel_reason" in cause:
                                need(
                                    cause["error_type"] == "QueryCanceledError"
                                    and cause.get("sqlstate") == "57014"
                                    and type(cause["cancel_reason"]) is str
                                    and cause["cancel_reason"]
                                    in {"statement_timeout", "user_request", "unknown"},
                                    "native Postgres cancellation reason",
                                )
                            if "site" in cause:
                                cause_site = cause["site"]
                                closed(cause_site, "file line function")
                                need(
                                    cause_site["file"] in (*FILES, "unknown")
                                    and type(cause_site["line"]) is int
                                    and 0 <= cause_site["line"] < 2**31
                                    and isinstance(cause_site["function"], str)
                                    and re.fullmatch(
                                        r"[A-Za-z_][A-Za-z_0-9]{0,63}", cause_site["function"]
                                    ),
                                    "native failure cause site",
                                )
                            need(
                                isinstance(cause["error_type"], str)
                                and re.fullmatch(
                                    r"[A-Za-z_][A-Za-z_0-9]{0,63}", cause["error_type"]
                                )
                                and (
                                    cause["errno"] is None
                                    or type(cause["errno"]) is int
                                    and -(2**31) <= cause["errno"] < 2**31
                                ),
                                "native driver failure cause",
                            )
                    site = failure["site"]
                    closed(site, "file line function")
                    need(
                        isinstance(failure["error_type"], str)
                        and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", failure["error_type"])
                        and failure["outcome"] in {"refused", "error", "cancelled"}
                        and site["file"] in (*FILES, "unknown")
                        and type(site["line"]) is int
                        and 0 <= site["line"] < 2**31
                        and isinstance(site["function"], str)
                        and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", site["function"]),
                        "native failure metadata",
                    )
                    error.native = failure
                except BaseException:  # noqa: BLE001 — diagnostics must not replace the original command failure.
                    pass
            # A terminal cleanup refusal is recorded, never retried as another mutation.
            if cleanup and self.o.leaders and self.o.leaders[-1]["terminal"] is not None:
                self.o.select(error)
                return {"incomplete": True, "task": task, "error_type": type(error).__name__}
            raise
        return read(target)

    def http(
        self, path: str, *, body: dict | None = None, token: str = "", tls: bool = False
    ) -> dict:
        import ssl

        import httpx

        port = self.s["ports"]["tls" if tls else "api"]
        verify = ssl.create_default_context(cafile=str(self.w / "ca.crt")) if tls else True
        context = httpx.Client(timeout=10, follow_redirects=False, trust_env=False, verify=verify)
        client = context.__enter__()
        try:
            response = client.request(
                "POST" if body is not None else "GET",
                f"{'https' if tls else 'http'}://127.0.0.1:{port}{path}",
                json=body,
                headers={"Authorization": "Bearer " + token} if token else {},
            )
            need(response.status_code == 200, "native HTTP refused")
            value = response.json()
            need(isinstance(value, dict), "native HTTP shape")
        except BaseException as error:
            try:
                context.__exit__(type(error), error, error.__traceback__)
            except BaseException as secondary:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.o.select(error)
                self.o.select(secondary)
            raise
        else:
            context.__exit__(None, None, None)
            self.o.monitor()
            return value

    def wait_api(self) -> None:
        import httpx

        end = min(time.monotonic() + 120, self.o.started + 7020)
        while True:
            self.o.monitor()
            try:
                value = self.http("/v1/health")
                ready = value.get("readiness", {})
                checks = ready.get("checks", {})
                required = {
                    "draining",
                    "clickhouse",
                    "migrations",
                    "revocation",
                    "keyring",
                    "tracker_registry",
                    "runtime_overrides",
                    "calibration",
                    "config_drift",
                    "cell",
                    "sandbox",
                }
                if self.settings["cell"]:
                    required.add("cell_marker")
                need(
                    value.get("ch_ok") is True
                    and value.get("pg_ok") is True
                    and value.get("storage_authority") == "postgres"
                    and value.get("pending_migrations") == []
                    and ready.get("ready") is True
                    and ready.get("failing") == []
                    and required <= set(checks)
                    and all(
                        checks[name].get("required", True) is True
                        and checks[name].get("ok") is True
                        for name in required
                    )
                    and all(
                        isinstance(check, dict)
                        and type(check.get("ok")) is bool
                        and (check.get("required", True) is False or check["ok"] is True)
                        for check in checks.values()
                    ),
                    "healthy ready API stores",
                )
                return
            except (OSError, httpx.TransportError, Refused):
                need(time.monotonic() < end, "API boot deadline")
                time.sleep(1)

    def start_api(self, stage: str) -> None:
        folder = self.w / stage
        folder.mkdir(mode=0o700)
        self.api = self.o.start(
            "api-" + stage,
            [
                self.s["python"],
                str(HERE / FILES[1]),
                "--task",
                "api",
                "--context",
                str(self.context),
                "--output",
                str(folder / "launch.json"),
            ],
            self.env(),
            persistent=True,
        )
        self.wait_api()
        self.launch = read(folder / "launch.json")
        self.launch_path = folder / "launch.json"

    def setup(self) -> None:
        import secrets

        # docker inspect is read-only and the input names exact positive IDs.
        self.inspect_stores()
        for role in ("pg_cell", "pg_directory", "ch_schema", "ch_telemetry"):
            output = self.w / (role + "-created.json")
            self.action("create", role=role)
            # Native action writes the authoritative creation receipt, not this observation.
            need(read(output)["created"] is True, "actual database creation receipt")
            self.created.append(role)
        cell = self.cli("cell", "credential", "new", settings={})
        operator = self.cli("cell", "credential", "new", "--operator", settings={})

        def credential(item, prefix):
            text = item["log"].read_text()
            match = re.search(r"^credential: (" + prefix + r"[0-9a-f]{128})$", text, re.M)
            need(match, "native credential output")
            return match[1]

        cell_token = credential(cell, "lcr_")
        self.operator = credential(operator, "lco_")
        p, db = self.s["ports"], self.s["databases"]
        self.settings = dict(
            ch_host="127.0.0.1",
            ch_port=self.s["ch"]["port"],
            ch_user=self.s["ch"]["username"],
            ch_password=self.s["ch"]["password"],
            ch_database=db["ch_schema"],
            ch_telemetry_database=db["ch_telemetry"],
            pg_dsn=pg_dsn(self.s["pg_admin_dsn"], db["pg_cell"]),
            storage_authority="postgres",
            pg_durability="synchronous",
            embedding_provider="fake",
            embedding_model="fake-ngram-1024",
            embedding_dim=1024,
            llm_provider="fake",
            llm_model="fake-echo-1",
            require_real_providers=False,
            background_workers="off",
            pg_delivery_batch_rows=500,
            auth_required=True,
            identity_provider="gateway",
            self_signup="off",
            mcp_auth_mode="required",
            jwt_secret=secrets.token_hex(32),
            scim_token="",
            cell="",
            cell_probe_port=p["probe"],
            cell_router_url=f"http://127.0.0.1:{p['router']}",
            cell_router_token=cell_token,
            router_operator_token="",
            public_base_url=f"https://127.0.0.1:{p['tls']}",
            build_commit=self.s["head"],
            bind_host="127.0.0.1",
            document_s3_bucket="",
        )
        router = dict(
            self.settings,
            cell_router_token="",
            router_pg_dsn=pg_dsn(self.s["pg_admin_dsn"], db["pg_directory"]),
            router_operator_sha256=__import__("hashlib").sha256(self.operator.encode()).hexdigest(),
            router_shared_host=f"127.0.0.1:{p['tls']}",
            router_upstream_url=f"http://127.0.0.1:{p['api']}",
            router_upstream_probe_url=f"http://127.0.0.1:{p['probe']}",
        )
        self.command_receipt("directory_migrate", self.cli("router", "migrate", settings=router))
        self.o.start(
            "router",
            [
                self.s["python"],
                "-m",
                "server.cli",
                "router",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(p["router"]),
            ],
            self.env(router),
            persistent=True,
        )
        self.certificate()
        self.start_api("before-cell")
        self.o.start(
            "proxy",
            [
                self.s["python"],
                str(HERE / FILES[2]),
                "--listen-port",
                str(p["tls"]),
                "--upstream-port",
                str(p["api"]),
                "--certificate",
                str(self.w / "server.crt"),
                "--key",
                str(self.w / "server.key"),
            ],
            self.env({}),
            persistent=True,
        )
        minted = self.cli(
            "mint-token",
            "--subject",
            "benchmark",
            "--org",
            self.org,
            "--namespaces",
            self.namespace,
            "--roles",
            "writer",
            "--service-account",
            "--ttl-days",
            "1",
            "--save",
            str(self.w / "writer.jwt"),
            "--json",
        )
        write(self.w / "mint-process.json", self.process_receipt(minted))
        retirement = self.action("mint-proof")
        metadata, self.token = mint_metadata(self.s, self.process_receipt(minted))
        self.s["mint_metadata"] = write(self.w / "mint-metadata.json", metadata)
        self.command_receipt(
            "mint", minted, retirement=retirement, metadata=self.s["mint_metadata"]
        )
        result = self.http(
            "/v1/namespace.register",
            token=self.token,
            body={
                "org": self.org,
                "namespace": self.namespace,
                "owner_email": "benchmark",
                "default_conflict_policy": "append_only",
            },
        )
        need(
            result.get("registered") is True and result.get("namespace") == self.namespace,
            "native namespace registration",
        )
        write(self.w / "namespace-register.json", result)
        self.receipt("namespace_register", result)
        self.receipt("authority_boot", self.action("bootstrap-proof"))
        import httpx

        end = min(time.monotonic() + 30, self.o.started + 7020)
        while True:
            self.o.monitor()
            try:
                self.http("/v1/health/deployment", tls=True)
                break
            except httpx.TransportError:
                need(time.monotonic() < end, "TLS listener startup")
                time.sleep(0.1)
        operator_settings = dict(
            self.settings, cell=self.cell, router_operator_token=self.operator, cell_router_token=""
        )
        self.command_receipt(
            "prepare",
            self.cli(
                "cell",
                "prepare",
                "--cell",
                self.cell,
                "--host",
                f"127.0.0.1:{p['tls']}",
                "--credential-sha256",
                __import__("hashlib").sha256(cell_token.encode()).hexdigest(),
                settings=operator_settings,
                seconds=600,
            ),
        )
        self.o.stop_one(self.api, api=True)
        self.settings["cell"] = self.cell
        self.start_api("serving-cell")
        # Read the actual source's widened interval in a bounded metadata child.
        interval = self.action("window")["seconds"]
        end = time.monotonic() + interval
        while time.monotonic() < end:
            self.o.monitor()
            time.sleep(1)
        self.command_receipt(
            "attach",
            self.cli(
                "cell",
                "attach",
                "--cell",
                self.cell,
                "--wait",
                "600",
                "--scan-budget",
                "600",
                "--ch-allow-user",
                self.s["ch"]["username"],
                settings=operator_settings,
                seconds=650,
            ),
        )
        self.s["api_instance_id"] = self.launch["server"]["instance_id"]
        self.context = self.w / "attached-context.json"
        write(self.context, self.s)
        self.cli_settings = dict(self.settings, cell_router_token="")
        self.attached = self.action("proof", settings=self.cli_settings)
        self.receipt("post_attach_proof", self.attached)

    def inspect_stores(self, *, cleanup: bool = False) -> None:
        for expected in self.s["stores"].values():
            command = self.o.command(
                "inspect",
                ["docker", "inspect", expected["container_id"]],
                self.env({}),
                cleanup=cleanup,
            )
            raw = json.loads(command["log"].read_text())
            need(len(raw) == 1, "one inspected container")
            actual = raw[0]
            need(
                actual["Id"] == expected["container_id"]
                and actual["Created"] == expected["created_at"]
                and actual["State"]["Running"] is True
                and actual["State"]["Pid"] == expected["host_pid"]
                and actual["HostConfig"]["Memory"] == expected["memory_limit_bytes"],
                "current owned container",
            )
            observed = []
            for internal, bindings in actual["NetworkSettings"]["Ports"].items():
                for binding in bindings or []:
                    observed.append(
                        {
                            "host": binding["HostIp"],
                            "host_port": int(binding["HostPort"]),
                            "internal_port": int(internal.split("/")[0]),
                            "protocol": internal.split("/")[1],
                        }
                    )
            need(
                sorted(observed, key=digest) == sorted(expected["ports"], key=digest),
                "actual loopback bindings",
            )
            need(
                expected["disk_path"] in {m["Source"] for m in actual["Mounts"]},
                "owned store filesystem",
            )
            identity = self.o.owned.identity(expected["host_pid"])
            need(
                identity
                and identity[1] == expected["start_ticks"]
                and os.stat(f"/proc/{expected['host_pid']}").st_uid == expected["uid"]
                and Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                == expected["boot_id"],
                "store process identity",
            )
        prefix = self.o.command(
            "keeper-prefix",
            [
                "docker",
                "exec",
                self.s["stores"]["clickhouse"]["container_id"],
                "clickhouse",
                "extract-from-config",
                "--config-file",
                "/etc/clickhouse-server/config.xml",
                "--key",
                "keeper_map_path_prefix",
            ],
            self.env({}),
            cleanup=cleanup,
        )
        need(
            prefix["log"].read_text().strip() == self.s["ch"]["keeper_prefix"],
            "actual Keeper prefix",
        )

    def certificate(self) -> None:
        def openssl(*args):
            self.o.command("openssl", ["openssl", *args], self.env({}))

        previous = os.umask(0o077)
        try:
            openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-noenc",
                "-keyout",
                str(self.w / "ca.key"),
                "-out",
                str(self.w / "ca.crt"),
                "-days",
                "1",
                "-subj",
                "/CN=local-trigger-ca",
            )
            openssl(
                "req",
                "-newkey",
                "rsa:2048",
                "-noenc",
                "-keyout",
                str(self.w / "server.key"),
                "-out",
                str(self.w / "server.csr"),
                "-subj",
                "/CN=127.0.0.1",
            )
            extension = self.w / "server.ext"
            extension.write_text("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
            openssl(
                "x509",
                "-req",
                "-in",
                str(self.w / "server.csr"),
                "-CA",
                str(self.w / "ca.crt"),
                "-CAkey",
                str(self.w / "ca.key"),
                "-CAcreateserial",
                "-out",
                str(self.w / "server.crt"),
                "-days",
                "1",
                "-extfile",
                str(extension),
            )
        finally:
            os.umask(previous)
        openssl("verify", "-CAfile", str(self.w / "ca.crt"), str(self.w / "server.crt"))
        dates = self.o.command(
            "certificate-dates",
            ["openssl", "x509", "-in", str(self.w / "server.crt"), "-noout", "-dates"],
            self.env({}),
        )
        values = dict(line.split("=", 1) for line in dates["log"].read_text().splitlines())
        parsed = {
            key: datetime.datetime.strptime(value, "%b %d %H:%M:%S %Y %Z")
            .replace(tzinfo=datetime.UTC)
            .isoformat()
            for key, value in values.items()
        }
        private = write(
            self.w / "tls-files.json",
            {
                "schema": 2,
                "files": [
                    {
                        "path": str(self.w / name),
                        "uid": (self.w / name).stat().st_uid,
                        "mode": (self.w / name).stat().st_mode & 0o777,
                        "bytes": (self.w / name).stat().st_size,
                        "sha256": sha(self.w / name),
                    }
                    for name in ("ca.key", "ca.crt", "server.key", "server.crt")
                ],
            },
        )
        self.tls = {
            "ca_certificate_sha256": sha(self.w / "ca.crt"),
            "server_certificate_sha256": sha(self.w / "server.crt"),
            "server_ip_san": "127.0.0.1",
            "not_before": parsed["notBefore"],
            "not_after": parsed["notAfter"],
            "listener": f"https://127.0.0.1:{self.s['ports']['tls']}",
            "upstream": f"http://127.0.0.1:{self.s['ports']['api']}",
            "private_files_receipt": private,
        }

    def measure(self) -> str:
        # Receipt construction is explicit; no old single-digest schema is accepted.
        config = ref(self.launch["configuration_artifact"])
        ownership = self.ownership()
        ownership_ref = write(self.w / "ownership.json", ownership)
        p, db = self.s["ports"], self.s["databases"]
        fixture = dict(
            schema=2,
            base_url=f"http://127.0.0.1:{p['api']}",
            pg_host="127.0.0.1",
            pg_port=url_port(self.s["pg_admin_dsn"]),
            pg_database=db["pg_cell"],
            pg_internal_port=5432,
            ch_host="127.0.0.1",
            ch_port=self.s["ch"]["port"],
            ch_database=db["ch_schema"],
            ch_telemetry_database=db["ch_telemetry"],
            cell=self.cell,
            org=self.org,
            namespace=self.namespace,
            deployment_id=str(self.attached["deployment_id"]),
            epoch=self.attached["epoch"],
            instance_id=self.launch["server"]["instance_id"],
            build_commit=self.s["head"],
            source_tree=self.s["tree"],
            launch_manifest_sha256=sha(self.launch_path),
            ownership_receipt_sha256=ownership_ref["sha256"],
            background_workers="off",
            controller_excluded=True,
            **{
                k: config[k]
                for k in (
                    "api_configuration_sha256",
                    "cli_configuration_sha256",
                    "shared_configuration_sha256",
                )
            },
        )
        write(self.w / "fixture.json", fixture)
        self.action("resolve", settings=self.cli_settings)
        metadata = ref(self.s["mint_metadata"])
        need(verify_token(metadata) == self.token, "native bearer unchanged before driver")
        item = self.o.start(
            "driver",
            [
                self.s["python"],
                str(HERE / FILES[3]),
                "--context",
                str(self.context),
                "--capture-directory",
                str(self.w / "delivery-intent"),
                "--",
                "--base-url",
                fixture["base_url"],
                "--pg-dsn",
                self.cli_settings["pg_dsn"],
                "--token",
                self.token,
                "--org",
                self.org,
                "--namespace",
                self.namespace,
                "--fixture-manifest",
                str(self.w / "fixture.json"),
                "--launch-manifest",
                str(self.launch_path),
                "--output",
                str(self.w / "driver.jsonl"),
            ],
            self.env(self.cli_settings),
        )
        write(
            self.w / "driver-birth.json",
            dict(self.process_receipt(item), schema=2, fixture_id=self.s["fixture_id"]),
        )
        while self.o.poll(item) is None:
            self.o.monitor()
            time.sleep(0.05)
        write(
            self.w / "driver-finished.json",
            dict(self.process_receipt(item), schema=2, fixture_id=self.s["fixture_id"]),
        )
        need(verify_token(metadata) == self.token, "native bearer unchanged after driver")
        lines = item["log"].read_bytes().splitlines()
        need(lines and len(lines[-1]) <= 65536, "driver final result")
        result = json.loads(lines[-1])
        need(
            result.get("verdict") in {"PASS", "FAIL"}
            and item["process"].returncode == (0 if result["verdict"] == "PASS" else 1),
            "completed driver result",
        )
        return result["verdict"]

    def process_receipt(self, item: dict) -> dict:
        identity = item["identity"]
        args = list(item["argv"])
        for index, value in enumerate(args[:-1]):
            if value in {"--token", "--pg-dsn"}:
                args[index + 1] = "<redacted>"
        return {
            "role": item["role"],
            "pid": item["process"].pid,
            "start_ticks": identity[1],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "uid": os.geteuid(),
            "cwd": self.s["root"],
            "executable": item["argv"][0],
            "executable_sha256": sha(Path(item["argv"][0]))
            if Path(item["argv"][0]).is_absolute()
            else sha(Path(__import__("shutil").which(item["argv"][0]))),
            "source_sha256": digest(self.s["source_hashes"]),
            "configuration_sha256": item["configuration_sha256"],
            "redacted_argv": args,
            "launch_receipt": item["launch_receipt"],
            "terminal_receipt": item["terminal_receipt"],
        }

    def ownership(self) -> dict:
        # All entries are observations from completed setup, never a future driver PID.
        need(
            len(self.o.leaders) <= 64 and len(self.attached["keeper_paths"]) <= 64,
            "ownership entry bounds",
        )
        return {
            "schema": 2,
            "fixture_id": self.s["fixture_id"],
            "source": {
                "root": self.s["root"],
                "head": self.s["head"],
                "tree": self.s["tree"],
                "source_hashes": self.s["source_hashes"],
                "driver_sha256": self.s["source_hashes"]["driver"],
                "launcher_sha256": self.s["source_hashes"][FILES[0]],
                "proxy_sha256": self.s["source_hashes"][FILES[2]],
                "owned_children_sha256": self.s["source_hashes"]["owned_children.py"],
                "python_executable": self.s["python"],
                "python_sha256": self.s["python_sha256"],
                "lockfile_sha256": self.s["lockfile_sha256"],
                "dependency_receipt": self.s["dependency_receipt"],
            },
            "stores": [
                {k: v for k, v in s.items() if k != "disk_path"} for s in self.s["stores"].values()
            ],
            "created_databases": [
                {
                    "role": role,
                    "store_container_id": read(self.w / (role + "-created.json"))[
                        "store_container_id"
                    ],
                    "database": self.s["databases"][role],
                    "created_at": read(self.w / (role + "-created.json"))["created_at"],
                    "creation_receipt": {
                        "path": str(self.w / (role + "-created.json")),
                        "sha256": sha(self.w / (role + "-created.json")),
                    },
                    "drop_required": True,
                }
                for role in self.created
            ],
            "keeper_paths": [
                dict(
                    row,
                    store_container_id=self.s["stores"]["clickhouse"]["container_id"],
                    creation_receipt={
                        "path": str(self.w / "ch_schema-created.json"),
                        "sha256": sha(self.w / "ch_schema-created.json"),
                    },
                    drop_required=True,
                )
                for row in self.attached["keeper_paths"]
            ],
            "processes": [self.process_receipt(item) for item in self.o.leaders],
            "tls": self.tls,
            "native_receipts": self.receipts,
            "controller_excluded": True,
            "resource_policy": POLICY,
        }

    def provenance(self) -> None:
        self.action("provenance", settings=self.cli_settings)

    def block_teardown(self, error: BaseException) -> None:
        # Retain the mutation barrier even if the negative receipt cannot be created.
        self.teardown_incomplete = True
        self.o.select(error)
        try:
            if not (self.w / "cleanup-incomplete.json").exists():
                write(
                    self.w / "cleanup-incomplete.json",
                    {
                        "schema": 2,
                        "fixture_id": self.s["fixture_id"],
                        "reason": "required cleanup evidence incomplete; stores retained",
                        "utc": utc(),
                    },
                )
        except BaseException as secondary:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
            self.o.select(secondary)

    def retirement_evidence(self, retired: dict, final: list[dict]) -> None:
        closed(retired, "schema fixture_id retired verified utc")
        need(
            type(retired["schema"]) is int
            and retired["schema"] == 2
            and retired["fixture_id"] == self.s["fixture_id"]
            and retired["verified"] is True
            and type(retired["retired"]) is list
            and len(retired["retired"]) == len(final),
            "acknowledged complete final retirement",
        )
        for value, item in zip(retired["retired"], final, strict=True):
            closed(value, "role terminal participant_id state version birth")
            equal(value["role"], item["role"])
            equal(value["terminal"], item["terminal_receipt"])
            equal(value["birth"], ref(item["launch_receipt"]))
            terminal = ref(item["terminal_receipt"])
            need(
                type(terminal["returncode"]) is int
                and terminal["returncode"] == item["process"].returncode,
                "final retirement terminal binding",
            )
            need(value["state"] == "retired", "positive native retirement state")
            identifier(value["participant_id"])
            integer(value["version"], 1, 2**63 - 1)

    def teardown(self) -> None:
        # Only creation receipts authorize deletion, even after partial setup.
        if not any(
            (self.w / (role + suffix)).exists()
            for role in self.s["databases"]
            for suffix in ("-intent.json", "-created.json")
        ):
            return
        self.inspect_stores(cleanup=True)
        for port in self.s["ports"].values():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                need(probe.connect_ex(("127.0.0.1", port)) != 0, "fixture port remains open")
        quiescent = self.action("quiescent", settings={}, cleanup=True)
        need(
            quiescent.get("ch_writers") == quiescent.get("pg_sessions") == 0,
            "server writer settlement unknown",
        )
        if self.teardown_incomplete or (self.w / "cleanup-incomplete.json").exists():
            self.teardown_incomplete = True
            self.o.select(Refused("known incomplete teardown; no mutation retry"))
            return
        if not (self.w / "cleanup-keeper-paths.json").exists():
            write(self.w / "cleanup-keeper-paths.json", {"keeper_paths": quiescent["keeper_paths"]})
        final = [item for item in self.o.leaders if item["role"].startswith(("api-", "driver-"))]
        if final:
            try:
                if self.retirement_receipt is None:
                    need(
                        not self.retirement_attempted
                        and not (self.w / "final-retirement.json").exists(),
                        "unacknowledged final retirement artifact; no retry",
                    )
                    self.retirement_attempted = True
                    need(
                        all(
                            item["terminal_receipt"] is not None
                            and item["launch_receipt"] is not None
                            for item in final
                        ),
                        "final native process receipts",
                    )
                    write(
                        self.w / "final-participant-births.json",
                        {
                            "processes": [
                                {
                                    "role": item["role"],
                                    "birth": item["launch_receipt"],
                                    "terminal": item["terminal_receipt"],
                                    "returncode": item["process"].returncode,
                                }
                                for item in final
                            ]
                        },
                    )
                    retired = self.action("final-retirement", settings={}, cleanup=True)
                    self.retirement_evidence(retired, final)
                    receipt = {
                        "path": str(self.w / "final-retirement.json"),
                        "sha256": sha(self.w / "final-retirement.json"),
                    }
                    equal(ref(receipt), retired)
                    self.retirement_receipt = receipt
                self.retirement_evidence(ref(self.retirement_receipt), final)
            except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
                self.block_teardown(error)
                return
        for role in self.s["databases"]:
            if (self.w / (role + "-intent.json")).exists() and not (
                self.w / (role + "-created.json")
            ).exists():
                state = self.action("resolve-creation", role=role, settings={}, cleanup=True)
                if state.get("present") is not False:
                    self.block_teardown(Refused("uncertain database ownership; leave untouched"))
                    return
        for role in ("ch_telemetry", "ch_schema", "pg_directory", "pg_cell"):
            created = self.w / (role + "-created.json")
            if created.exists() and not (self.w / ("drop-" + role + ".json")).exists():
                result = self.action("drop", role=role, settings={}, cleanup=True)
                if result.get("dropped") is not True:
                    self.block_teardown(Refused("native scratch cleanup incomplete"))
                    # A failed DROP may still run on the server; prove settlement afresh.
                    settled = self.action("quiescent", settings={}, cleanup=True)
                    need(
                        settled.get("ch_writers") == settled.get("pg_sessions") == 0,
                        "drop outcome still running",
                    )
                    return
        if not (self.w / "keeper-proof.json").exists():
            result = self.action("keeper-proof", settings={}, cleanup=True)
            if result.get("keeper_paths_absent") is not True:
                self.block_teardown(Refused("owned Keeper cleanup incomplete"))
                return
        if not (self.w / "teardown.json").exists():
            write(
                self.w / "teardown.json",
                {
                    "schema": 2,
                    "fixture_id": self.s["fixture_id"],
                    "dropped": [
                        role
                        for role in self.s["databases"]
                        if (self.w / ("drop-" + role + ".json")).exists()
                    ],
                    "owned_ports_closed": True,
                    "pg_sessions_closed": True,
                    "job_container_cleanup_required": True,
                    "utc": utc(),
                },
            )


def url_port(dsn: str) -> int:
    from urllib.parse import urlsplit

    port = urlsplit(dsn).port
    need(port is not None, "explicit PG port")
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task",
        required=True,
        choices=[
            "api",
            "create",
            "drop",
            "resolve-creation",
            "mint-proof",
            "final-retirement",
            "proof",
            "provenance",
            "bootstrap-proof",
            "quiescent",
            "keeper-proof",
            "window",
            "resolve",
        ],
    )
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--role", default="")
    args = parser.parse_args()
    try:
        context = read(args.context)
        sys.path.insert(0, context["root"])
        if args.task == "api":
            api(context, args.output)
        elif args.task in {"create", "drop"}:
            need(args.role in context["databases"], "fixed database role")
            asyncio.run(stores(context, args.role, args.output, drop=args.task == "drop"))
        elif args.task == "resolve-creation":
            need(args.role in context["databases"], "fixed database role")
            asyncio.run(resolve_creation(context, args.role, args.output))
        elif args.task == "window":
            from server.cli.cell_attach_beats import widened_window_s

            write(args.output, {"seconds": widened_window_s()})
        elif args.task == "mint-proof":
            asyncio.run(mint_proof(context, args.output))
        elif args.task == "provenance":
            asyncio.run(independent_provenance(context, args.output))
        elif args.task == "final-retirement":
            asyncio.run(final_retirement(context, args.output))
        elif args.task == "resolve":
            from server.settings import settings

            codec_contract()
            config = ref(
                read(args.output.parent / "serving-cell/launch.json")["configuration_artifact"]
            )
            actual = settings.model_dump(mode="json")
            need(
                actual["cell_router_token"] == actual["router_operator_token"] == "",
                "CLI credentials",
            )
            need(
                digest(actual) == config["cli_configuration_sha256"]
                and digest({k: v for k, v in actual.items() if k != "cell_router_token"})
                == config["shared_configuration_sha256"],
                "actual CLI configuration",
            )
            write(
                args.output,
                {
                    "schema": 2,
                    "cli_configuration_sha256": digest(actual),
                    "codec_protocol_sha256": PROTOCOL_SHA,
                    "verified": True,
                },
            )
        else:
            asyncio.run(
                proof(
                    context,
                    args.output,
                    bootstrap=args.task == "bootstrap-proof",
                    quiescent=args.task == "quiescent",
                    keeper=args.task == "keeper-proof",
                )
            )
    except BaseException as error:  # noqa: BLE001 — owned cleanup must finish while retaining the first failure.
        try:
            write(
                args.output.with_name(args.output.name + ".failure.json"),
                native_failure_metadata(error),
                limit=4096,
            )
        except BaseException:  # noqa: BLE001 — diagnostic output must not replace the native failure.
            pass
        print(type(error).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
