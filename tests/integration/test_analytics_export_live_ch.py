"""The analytics export's statements on a live ClickHouse: one UTC day of the
ledger summed per operation, the namespace snapshot, the exact columns that
leave Loom, hashed names, and Parquet output.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from server.core.ch import CHClient, get_client
from server.core.paths import REPO_ROOT
from server.insights.app.analytics_export import name_hash_key
from server.insights.store.analytics_export import (
    ENTITIES,
    NAME_COLUMNS,
    NAMESPACES_SQL,
    ROW_ORDER,
    STATEMENT_SETTINGS,
    USAGE_SQL,
    _hashed,
    name_hash_sql,
    parquet,
)
from tests.integration._resources import unique_org
from tests.siphash_reference import siphash_2_4_128

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

DAY = date(2026, 1, 2)
KEY = name_hash_key("test-key")

# Every column that leaves Loom. A column added to a statement fails here until
# it is added to this list, on purpose: the list is the export's allowlist.
COLUMNS = {
    "usage": [
        "org", "namespace", "agent", "operation", "kind", "transport", "traffic_class", "day",
        "ops", "errors", "server_errors", "slow", "input_tokens", "output_tokens",
        "embedding_tokens", "llm_calls", "cost_usd", "bytes_written", "latency_p50_ms",
        "latency_p95_ms", "latency_p99_ms", "users", "sessions", "snapshot_at",
    ],
    "namespaces": [
        "org", "namespace", "registered", "kind", "visibility", "active", "created_on",
        "live_memories", "last_memory_on", "day", "snapshot_at",
    ],
}  # fmt: skip


@pytest.fixture
async def ch() -> AsyncIterator[CHClient]:
    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


async def _file_rows(ch: CHClient, entity: str) -> list[dict[str, Any]]:
    """The rows of the Parquet file the export ships for `entity`, in file
    order, decoded by ClickHouse: what the warehouse loads, not raw bytes."""
    body = await parquet(entity, DAY, name_key=KEY, ch=ch)
    table = f"default.it_analytics_{uuid.uuid4().hex}"
    params = {"day": DAY, "k0": KEY[0], "k1": KEY[1]}
    await ch.execute(
        f"CREATE TABLE {table} ENGINE = Memory AS SELECT * FROM ({_hashed(entity)}) LIMIT 0",
        params,
    )
    try:
        await ch.client.raw_insert(table, insert_block=body, fmt="Parquet")
        return await ch.query_all(f"SELECT * FROM {table} SETTINGS max_threads = 1")
    finally:
        await ch.execute(f"DROP TABLE {table}")


def _event(org: str, ts: str, **kw: Any) -> dict[str, Any]:
    row = {
        "ts": datetime.fromisoformat(ts),
        "org": org,
        "namespace": "ns",
        "agent": "a",
        "user_id": "u1",
        "session_id": "s1",
        "operation": "memory.set",
        "kind": "request",
        "request_id": ts,
        "latency_ms": 10,
        "success": True,
        "transport": "http",
        "status_class": "ok",
        "budget_ms": 100,
        "input_tokens": 0,
        "upstream_cost_usd": Decimal(0),
    }
    return {**row, **kw}


async def test_usage_sums_one_utc_day_per_operation(ch: CHClient) -> None:
    org = unique_org("analytics")
    await ch.insert(
        "loom.agent_events",
        [
            _event(org, "2026-01-02 00:00:00", upstream_cost_usd=Decimal("0.25"), input_tokens=7),
            _event(
                org,
                "2026-01-02 12:00:00",
                user_id="u2",
                latency_ms=300,
                success=False,
                status_class="server",
            ),
            _event(org, "2026-01-02 23:59:59.999", user_id="-", session_id="-"),
            # Slow as the SLI counts it: an `ok` request past its budget. The
            # server error above at 300 ms is an error, not a slow request.
            _event(org, "2026-01-02 13:00:00", latency_ms=250),
            _event(org, "2026-01-01 23:59:59.999"),
            _event(org, "2026-01-03 00:00:00"),
            _event(org, "2026-01-02 08:00:00", operation="memory.get", transport="mcp"),
            # Ledgered from a raw /v1/me path: any string a caller sent.
            _event(org, "2026-01-02 09:00:00", operation="me.alice@example.com"),
            _event(org, "2026-01-02 09:30:00", operation="me.sharing-summary"),
        ],
    )
    # A profile that resolves names to columns before aliases gives the same rows.
    for prefer_column in (0, 1):
        rows = await ch.query_all(
            f"SELECT * FROM ({USAGE_SQL}) WHERE org = {{org:String}} "
            f"SETTINGS prefer_column_name_to_alias = {prefer_column}",
            {"day": DAY, "org": org},
        )
        assert sorted(r["operation"] for r in rows) == ["me", "memory.get", "memory.set"]
        by_op = {r["operation"]: r for r in rows}
        assert by_op["me"]["ops"] == 2
        s = by_op["memory.set"]
        assert (s["ops"], s["errors"], s["server_errors"], s["slow"]) == (4, 1, 1, 1)
        # Over the three `ok` requests (10, 10, 250 ms), not the 300 ms error.
        assert (s["latency_p50_ms"], s["latency_p99_ms"]) == (10, 250)
        assert (s["users"], s["sessions"]) == (2, 1)
        assert (s["input_tokens"], s["cost_usd"]) == (7, Decimal("0.25"))
        assert (s["namespace"], s["agent"], s["transport"], s["day"]) == ("ns", "a", "http", DAY)
        assert s["kind"] == "request"
        assert by_op["memory.get"]["ops"] == 1
    shipped = {r["operation"] for r in await _file_rows(ch, "usage")}
    assert "me" in shipped and not [op for op in shipped if op.startswith("me.")]


async def test_hashed_names_are_the_keyed_hash_of_the_stored_names(ch: CHClient) -> None:
    org = unique_org("analytics")
    await ch.insert("loom.agent_events", [_event(org, "2026-01-02 06:00:00")])
    params = {"day": DAY, "k0": KEY[0], "k1": KEY[1]}
    want = await ch.query_one(
        f"SELECT {name_hash_sql('{org:String}')} AS h, "
        f"{name_hash_sql("'ns'")} AS ns, {name_hash_sql("'a'")} AS a",
        {**params, "org": org},
    )
    rows = await ch.query_all(
        f"SELECT * FROM ({_hashed('usage')}) WHERE org = {{h:String}}", {**params, "h": want["h"]}
    )
    assert [(r["namespace"], r["agent"], r["ops"]) for r in rows] == [(want["ns"], want["a"], 1)]
    shipped = {r["org"] for r in await _file_rows(ch, "usage")}
    assert want["h"] in shipped and org not in shipped


@pytest.mark.parametrize("entity", sorted(ENTITIES))
async def test_the_file_is_in_hash_order_never_name_order(ch: CHClient, entity: str) -> None:
    org = unique_org("analytics")
    # Two names whose hash order is the reverse of their own: a file sorted by
    # the names puts their rows out of hash order.
    hashed = await ch.query_all(
        f"SELECT n, {name_hash_sql('n')} AS h FROM (SELECT concat('n', toString(number)) AS n "
        "FROM numbers(50))",
        {"k0": KEY[0], "k1": KEY[1]},
    )
    first = min(hashed, key=lambda r: r["n"])
    second = next(r for r in sorted(hashed, key=lambda r: r["n"]) if r["h"] < first["h"])
    names = [first["n"], second["n"]]
    await ch.insert(
        "loom.agent_events", [_event(org, "2026-01-02 07:00:00", namespace=n) for n in names]
    )
    await ch.insert(
        "loom.namespaces",
        [
            {"org": org, "namespace": n, "owner_email": "x@example.com", "kind": "memory"}
            for n in names
        ],
    )
    columns = ROW_ORDER[entity].split(", ")
    order = [tuple(str(r[c]) for c in columns) for r in await _file_rows(ch, entity)]
    assert order == sorted(order)
    # A profile that resolves names to columns before aliases sorts the same.
    for prefer_column in (0, 1):
        rows = await ch.query_all(
            f"{_hashed(entity)} SETTINGS prefer_column_name_to_alias = {prefer_column}",
            {"day": DAY, "k0": KEY[0], "k1": KEY[1]},
        )
        order = [tuple(str(r[c]) for c in columns) for r in rows]
        assert order == sorted(order), prefer_column


async def test_the_operator_guides_join_sql_computes_the_exported_hash(ch: CHClient) -> None:
    # The ```sql block of the operator guide, run as a warehouse would run it.
    guide = (REPO_ROOT / "docs/operations/analytics-export.md").read_text()
    (sql,) = re.findall(r"```sql\n(.*?)```", guide, re.S)
    secret = "s" * 32
    sql = sql.replace("FROM tenants", "FROM (SELECT 'tenant-1' AS tenant_id)")
    row = await ch.query_one(sql, {"key": secret})
    k0, k1 = name_hash_key(secret)
    want = await ch.query_one(f"SELECT {name_hash_sql("'tenant-1'")} AS h", {"k0": k0, "k1": k1})
    assert row["hashed_tenant_id"] == want["h"]


@pytest.mark.parametrize("name", ["", "ns", "agent-alice-laptop", "x" * 8, "tenant-" * 9])
async def test_a_hashed_name_is_reference_siphash_under_the_documented_key(
    ch: CHClient, name: str
) -> None:
    # Any system with the key reproduces the hash: SipHash-2-4-128 under the
    # first 16 bytes of SHA-256(key), as the operator guide documents.
    secret = "s" * 32
    k0, k1 = name_hash_key(secret)
    row = await ch.query_one(
        f"SELECT {name_hash_sql('{name:String}')} AS h", {"k0": k0, "k1": k1, "name": name}
    )
    key = hashlib.sha256(secret.encode()).digest()[:16]
    assert row["h"] == siphash_2_4_128(key, name.encode()).hex()


@pytest.mark.parametrize("entity", sorted(ENTITIES))
async def test_the_columns_that_leave_loom_are_exactly_the_allowlist(
    ch: CHClient, entity: str
) -> None:
    params = {"day": DAY, "k0": KEY[0], "k1": KEY[1]}
    for sql in (ENTITIES[entity], _hashed(entity)):
        rows = await ch.query_all(f"DESCRIBE TABLE ({sql})", params)
        assert [r["name"] for r in rows] == COLUMNS[entity]


# Profile settings that change a statement's column types or join and alias
# resolution, each tried on and off around the statement as shipped.
PROFILE_SETTINGS = (
    "join_use_nulls",
    "prefer_column_name_to_alias",
    "cast_keep_nullable",
    "aggregate_functions_null_for_empty",
)


# Profile settings that cut a result short with no error when left to the server.
TRUNCATING_PROFILES = (
    {"limit": 1},
    {"offset": 1},
    {"max_result_rows": 1, "result_overflow_mode": "break"},
    {"max_rows_to_group_by": 1, "group_by_overflow_mode": "any"},
)


@pytest.mark.parametrize("profile", TRUNCATING_PROFILES, ids=lambda p: ",".join(p))
async def test_a_profile_never_cuts_the_file_short(
    ch: CHClient, profile: dict[str, object]
) -> None:
    org = unique_org("analytics")
    await ch.insert(
        "loom.agent_events",
        [_event(org, "2026-01-02 05:00:00", namespace=f"n{i}") for i in range(3)],
    )
    params = {"day": DAY, "k0": KEY[0], "k1": KEY[1]}
    shipped = f"{_hashed('usage')} {STATEMENT_SETTINGS}"
    whole = await ch.client.query(shipped, parameters=params)
    # A whole file or an error: never a partial file, which an upload would keep.
    try:
        under_profile = await ch.client.query(shipped, parameters=params, settings=profile)
    except Exception as e:  # noqa: BLE001 — refusing is the other acceptable outcome
        assert "TOO_MANY_ROWS" in str(e) or "exceeded" in str(e), e
        return
    assert len(under_profile.result_rows) == len(whole.result_rows) >= 3


@pytest.mark.parametrize("entity", sorted(ENTITIES))
async def test_the_file_schema_does_not_depend_on_the_profile(ch: CHClient, entity: str) -> None:
    params = {"day": DAY, "k0": KEY[0], "k1": KEY[1]}
    shipped = f"{_hashed(entity)} {STATEMENT_SETTINGS}"
    default = await ch.query_all(f"DESCRIBE TABLE ({shipped})", params)
    types = {r["name"]: r["type"] for r in default}
    # The hashes are plain strings: not LowCardinality (its dictionary encoding
    # is the release's choice), not Nullable.
    assert all(types[c] == "String" for c in NAME_COLUMNS[entity]), types
    for values in itertools.product((0, 1), repeat=len(PROFILE_SETTINGS)):
        profile = ", ".join(f"{s} = {v}" for s, v in zip(PROFILE_SETTINGS, values, strict=True))
        rows = await ch.query_all(f"DESCRIBE TABLE ({shipped}) SETTINGS {profile}", params)
        assert {r["name"]: r["type"] for r in rows} == types, profile


async def test_namespaces_lists_registered_and_unregistered_with_live_counts(
    ch: CHClient,
) -> None:
    org = unique_org("analytics")
    old, new = datetime(2026, 1, 1), datetime(2026, 1, 2)
    reg = {"org": org, "owner_email": "x@example.com", "created_at": new}
    await ch.insert(
        "loom.namespaces",
        [
            # Two registry versions: the newer one (by updated_at) is the one exported.
            {**reg, "namespace": "kb", "kind": "memory", "updated_at": old},
            {**reg, "namespace": "kb", "kind": "knowledge", "updated_at": new},
            {**reg, "namespace": "mem", "kind": "memory", "updated_at": new},
        ],
    )
    base = {"agent": "a", "user_id": "-", "session_id": "-", "memory_type": "fact", "version": 1}
    live = {**base, "tombstoned_at": None}
    await ch.insert(
        "loom.memory",
        [
            {**live, "org": org, "namespace": "mem", "memory_key": "k1", "value": "v"},
            {**live, "org": org, "namespace": "mem", "memory_key": "k2", "value": "v"},
            # Deleted the way memory.delete deletes: a live version 1, then a
            # tombstoned version 2 over it. FINAL must pick version 2 before the
            # live filter, or version 1 would still count.
            {**live, "org": org, "namespace": "mem", "memory_key": "k3", "value": "v"},
            {
                **base,
                "version": 2,
                "tombstoned_at": new,
                "org": org,
                "namespace": "mem",
                "memory_key": "k3",
                "value": "v",
            },
            {**live, "org": org, "namespace": "loose", "memory_key": "k1", "value": "v"},
        ],
    )
    for join_use_nulls, prefer_column in itertools.product((0, 1), (0, 1)):
        rows = await ch.query_all(
            f"SELECT * FROM ({NAMESPACES_SQL}) WHERE org = {{org:String}} "
            f"SETTINGS join_use_nulls = {join_use_nulls}, "
            f"prefer_column_name_to_alias = {prefer_column}",
            {"day": DAY, "org": org},
        )
        _assert_namespaces({r["namespace"]: r for r in rows})


def _assert_namespaces(got: dict[str, dict[str, Any]]) -> None:
    assert sorted(got) == ["kb", "loose", "mem"]
    assert (got["kb"]["registered"], got["kb"]["kind"], got["kb"]["live_memories"]) == (
        True,
        "knowledge",
        0,
    )
    assert got["kb"]["last_memory_on"] is None
    assert got["kb"]["created_on"] == date(2026, 1, 2)
    assert (got["mem"]["live_memories"], got["mem"]["kind"]) == (2, "memory")
    loose = got["loose"]
    assert (loose["registered"], loose["kind"], loose["created_on"]) == (False, None, None)
    assert loose["live_memories"] == 1


@pytest.mark.parametrize("entity", sorted(ENTITIES))
async def test_each_entity_is_a_parquet_file(ch: CHClient, entity: str) -> None:
    body = await parquet(entity, DAY, name_key=KEY, ch=ch)
    assert body[:4] == b"PAR1" and body[-4:] == b"PAR1"
