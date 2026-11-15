"""The org scan and the ClickHouse writer check (server/cli/cell_scan.py,
server/cli/cell_writers.py) against a real ClickHouse and Postgres, on one host: the table
list and its judging, the orgs a scan finds and the ones it names malformed or too long, the
Postgres skip scan's plan and its index refusal, and the writer check on real users, grants
and objects. The configured ClickHouse databases are scratch databases of the test's own and
the Postgres database is the test's own, each dropped afterwards. A cluster of several hosts
is covered by the unit tests' fake. Needs LOOM_PG_DSN and LOOM_CH_HOST."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from server.cli import cell_scan, cell_writers
from server.cli.cell_scan import Budget, Refused, Table, TableList
from server.core import cell, ch_names, pg
from server.core.ch import get_client
from server.settings import settings
from tests.integration._pg_database import own_database
from tests.unit.test_cell_scan_tables import _LOGGED, closure


@pytest.fixture
async def ch() -> AsyncIterator[Any]:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


@pytest.fixture
async def scratch(ch: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[str, str]]:
    """Two scratch databases standing in for the configured schema and telemetry ones."""
    tag = uuid.uuid4().hex[:10]
    schema, telemetry = f"loom_it_scan_{tag}", f"loom_it_scant_{tag}"
    try:
        for db in (schema, telemetry):
            await ch.execute(f"CREATE DATABASE {db}")
        monkeypatch.setattr(settings, "ch_database", schema)
        monkeypatch.setattr(settings, "ch_telemetry_database", telemetry)
        yield schema, telemetry
    finally:
        for db in (schema, telemetry):
            await ch.execute(f"DROP DATABASE IF EXISTS {db} SYNC")


@pytest.fixture
async def own_pg(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    import asyncpg

    async with own_database() as dsn:
        monkeypatch.setattr(settings, "pg_dsn", dsn)
        await pg.close_pool()
        conn = await asyncpg.connect(dsn)
        try:
            yield conn
        finally:
            await conn.close()


def _mergetree(table: str, org_type: str = "String") -> str:
    return f"CREATE TABLE {table} (org {org_type}, id UInt64) ENGINE = MergeTree ORDER BY id"


async def test_the_listing_judges_every_org_table(
    ch: Any, scratch: tuple[str, str], own_pg: Any
) -> None:
    schema, telemetry = scratch
    for sql in (
        _mergetree(f"{schema}.memory"),
        _mergetree(f"{schema}.agent_events"),
        _mergetree(f"{schema}.stray"),
        f"CREATE TABLE {schema}.held (org String) ENGINE = Memory",
        f"CREATE VIEW {schema}.memory_v AS SELECT org FROM {schema}.memory",
        f"CREATE TABLE {schema}.no_org (id UInt64) ENGINE = MergeTree ORDER BY id",
        _mergetree(f"{telemetry}.sli_minute"),
    ):
        await ch.execute(sql)
    topology = await cell_scan.read_topology(ch)
    assert not topology.multi
    listed = await cell_scan.list_tables(ch, own_pg, topology, {})
    assert [str(r) for r in listed.refusals] == [
        "clickhouse loom.held: engine Memory holds an org column the scan cannot read",
        "clickhouse loom.stray: missing from erasure's table classification",
    ]
    # The log tables are listed, judged and left unscanned; the view stores no rows.
    assert listed.clickhouse == [Table(schema, "memory", "MergeTree")]
    assert "loom.tenancy" in listed.postgres and "loom.memory_current" in listed.postgres


async def test_a_classified_table_the_listing_missed_refuses(
    ch: Any, scratch: tuple[str, str]
) -> None:
    schema, _ = scratch
    await ch.execute(_mergetree(f"{schema}.memory"))
    refusals = await cell_scan.reverse_clickhouse(ch, [], {"loom.memory", "loom.absent"})
    assert [str(r) for r in refusals] == [
        "clickhouse loom.memory: has an org column the table listing did not return"
    ]


async def test_the_scan_finds_judges_and_names_every_org(
    ch: Any, scratch: tuple[str, str], own_pg: Any
) -> None:
    schema, _ = scratch
    await ch.execute(_mergetree(f"{schema}.memory"))
    await ch.execute(_mergetree(f"{schema}.chunks", "LowCardinality(String)"))
    long = "x" * (cell.ORG_MAX_BYTES + 1)
    await ch.execute(
        f"INSERT INTO {schema}.memory (org, id) SETTINGS async_insert = 0 VALUES "
        "('acme', 1), ('acme', 2), ('', 3), ('-', 4), (unhex('FF'), 5), ('a\\0b', 6), "
        "({long:String}, 7)",
        {"long": long},
    )
    await own_pg.execute("INSERT INTO loom.tenancy (org) VALUES ('acme'), ('gamma'), ('-'), ('*')")
    tables = TableList(
        clickhouse=[Table(schema, "memory", "MergeTree"), Table(schema, "chunks", "MergeTree")],
        postgres=["loom.tenancy", "loom.memory_current"],
    )
    topology = await cell_scan.read_topology(ch)
    result = await cell_scan.scan(ch, tables, topology, Budget(600), configured=["delta"])
    assert result.orgs == {
        "acme": {"clickhouse loom.memory", "postgres loom.tenancy"},
        "gamma": {"postgres loom.tenancy"},
        "delta": {"configured"},
    }
    assert result.malformed == {"clickhouse loom.memory": ["ff", "'a\\x00b'"]}
    assert result.too_long == {long: {"clickhouse loom.memory"}}
    assert 0 < result.elapsed_s < 600


async def test_the_postgres_scan_skips_through_the_org_index(own_pg: Any) -> None:
    await own_pg.execute(
        "INSERT INTO loom.tenancy (org) SELECT 'org-' || (i % 50) || '-' || i "
        "FROM generate_series(1, 20000) i"
    )
    await own_pg.execute("ANALYZE loom.tenancy")
    plan = json.loads(
        await own_pg.fetchval("EXPLAIN (FORMAT JSON) " + cell_scan.pg_skip_scan_sql("loom.tenancy"))
    )
    nodes = []

    def walk(node: dict[str, Any]) -> None:
        nodes.append(node)
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    scans = [n for n in nodes if "Relation Name" in n]
    # The first org and each next one: both probes of the primary key, no full read.
    assert [(n["Node Type"], n["Index Name"]) for n in scans] == [
        ("Index Only Scan", "tenancy_pkey"),
        ("Index Only Scan", "tenancy_pkey"),
    ], scans
    assert len(await cell_scan._pg_orgs("loom.tenancy", Budget(60))) == 20000
    # Every org-keyed table of the schema has the index the scan needs.
    names, refusals = await cell_scan.list_postgres(own_pg)
    assert refusals == []
    for name in names:
        assert await own_pg.fetchval(cell_scan._PG_ORG_INDEX, name), name


async def test_a_postgres_table_without_a_plain_org_index_refuses(own_pg: Any) -> None:
    await own_pg.execute("CREATE TABLE loom.scan_probe (org text, id int PRIMARY KEY)")
    await own_pg.execute("CREATE INDEX ON loom.scan_probe (lower(org))")
    await own_pg.execute("CREATE INDEX ON loom.scan_probe (org) WHERE id > 0")
    tables = TableList(postgres=["loom.scan_probe"])
    with pytest.raises(Refused, match="postgres loom.scan_probe: has no plain btree index"):
        await cell_scan.scan(
            get_client(), tables, cell_scan.Topology("", frozenset(), 0), Budget(60)
        )


_PASSWORD = "x{tag}"


@pytest.fixture
async def users(ch: Any) -> AsyncIterator[dict[str, str]]:
    tag = uuid.uuid4().hex[:8]
    names = {k: f"loom_it_{k}_{tag}" for k in ("writer", "narrow", "remote", "creator")}
    try:
        for name in names.values():
            password = _PASSWORD.format(tag=tag)
            await ch.execute(f"CREATE USER {name} IDENTIFIED WITH sha256_password BY '{password}'")
        yield {**names, "password": _PASSWORD.format(tag=tag)}
    finally:
        for name in names.values():
            await ch.execute(f"DROP USER IF EXISTS {name}")


async def test_the_writer_check_names_a_writer_and_an_object(
    ch: Any, scratch: tuple[str, str], users: dict[str, str]
) -> None:
    schema, _ = scratch
    side = f"{schema}_side"
    await ch.execute(_mergetree(f"{schema}.memory"))
    await ch.execute(f"GRANT INSERT ON {schema}.* TO {users['writer']}")
    try:
        await ch.execute(f"CREATE DATABASE {side}")
        await ch.execute(
            f"CREATE TABLE {side}.buf AS {schema}.memory "
            f"ENGINE = Buffer({schema}, memory, 1, 10, 100, 10000, 1000000, 10000000, 100000000)"
        )
        topology = await cell_scan.read_topology(ch)
        others = {str(r["name"]) for r in await ch.query_all("SELECT name FROM system.users")} - {
            users["writer"]
        }
        refusals = await cell_writers.check_writers(
            ch, topology, {}, cell_writers.Allowed(users=frozenset(others))
        )
        assert [str(r) for r in refusals if schema in str(r) or users["writer"] in str(r)] == [
            f"user {users['writer']}: holds INSERT on {schema}.*: name it with --ch-allow-user",
            f"object {side}.buf: a Buffer table writing a configured database: name it with "
            "--ch-allow-object",
        ]
        allowed = cell_writers.Allowed(
            users=frozenset(others | {users["writer"]}), objects=frozenset({f"{side}.buf"})
        )
        refusals = await cell_writers.check_writers(ch, topology, {}, allowed)
        assert not [r for r in refusals if schema in str(r) or users["writer"] in str(r)]
    finally:
        await ch.execute(f"DROP DATABASE IF EXISTS {side} SYNC")


async def test_a_user_that_creates_tables_anywhere_is_a_writer(
    ch: Any, scratch: tuple[str, str], users: dict[str, str]
) -> None:
    """A Distributed table on another database forwards its inserts to a remote shard as the
    cluster's configured user, unchecked against the destination, so CREATE TABLE anywhere
    writes the configured databases. A Buffer table's destination is checked: the insert
    through one refuses, which this pins for the day a server changes it."""
    from tests.integration._resources import ch_client_from_env

    schema, _ = scratch
    side = f"{schema}_side"
    creator = users["creator"]
    await ch.execute(_mergetree(f"{schema}.memory"))
    try:
        await ch.execute(f"CREATE DATABASE {side}")
        await ch.execute(f"GRANT CREATE TABLE, INSERT, SELECT ON {side}.* TO {creator}")
        topology = await cell_scan.read_topology(ch)
        refusals = await cell_writers.check_writers(ch, topology, {}, cell_writers.Allowed())
        assert f"user {creator}: holds CREATE TABLE on {side}.*: name it with --ch-allow-user" in [
            str(r) for r in refusals
        ]
        raw = ch_client_from_env(username=creator, password=users["password"])
        try:
            raw.command(
                f"CREATE TABLE {side}.buf (org String, id UInt64) "
                f"ENGINE = Buffer({schema}, memory, 1, 0, 1, 1, 1, 1, 1)"
            )
            with pytest.raises(Exception, match="ACCESS_DENIED|Not enough privileges"):
                raw.command(f"INSERT INTO {side}.buf VALUES ('acme', 1)")
        finally:
            raw.close()
        assert await ch.query_all(f"SELECT org FROM {schema}.memory") == []
    finally:
        await ch.execute(f"DROP DATABASE IF EXISTS {side} SYNC")


async def test_grants_are_read_as_the_server_keeps_them(
    ch: Any, scratch: tuple[str, str], users: dict[str, str]
) -> None:
    """From `system.grants` and `system.role_grants`: a per-table grant is not one on the
    database, `READ ON REMOTE` reads as REMOTE, and a role granted but not a default role
    still makes its holder a writer, since the user can set it."""
    schema, _ = scratch
    role = f"{users['writer']}_role"
    await ch.execute(_mergetree(f"{schema}.memory"))
    await ch.execute(f"GRANT SELECT ON {schema}.memory TO {users['narrow']}")
    await ch.execute(f"GRANT READ ON REMOTE TO {users['remote']}")
    try:
        await ch.execute(f"CREATE ROLE {role}")
        await ch.execute(f"GRANT INSERT ON {schema}.* TO {role}")
        await ch.execute(f"GRANT {role} TO {users['writer']}")
        await ch.execute(f"SET DEFAULT ROLE NONE TO {users['writer']}")
        topology = await cell_scan.read_topology(ch)
        (grants,) = (await cell_writers._host_grants(ch, topology, {})).values()
        assert f"SELECT ON {schema}.*" in cell_writers.own_problems(
            grants[users["narrow"]], topology, [schema]
        )
        assert cell_writers.holds(grants[users["remote"]], "REMOTE"), grants[users["remote"]]
        assert cell_writers.writer_reason(grants[users["writer"]], [schema]) == (
            f"holds INSERT on {schema}.*"
        )
        refusals = await cell_writers.check_writers(ch, topology, {}, cell_writers.Allowed())
        assert any(str(r).startswith(f"user {users['writer']}: holds INSERT") for r in refusals)
    finally:
        await ch.execute(f"DROP ROLE IF EXISTS {role}")


async def test_every_table_a_view_fills_from_a_log_table_is_a_log_table(ch: Any) -> None:
    """The schema's own views, as `system.tables` holds them. A refreshable view is not in
    its source's `dependencies_table` (only an insert-triggered one is), so each view's
    query is read instead."""
    views = await ch.query_all(
        "SELECT create_table_query, as_select FROM system.tables "
        "WHERE database IN {dbs:Array(String)} AND engine = 'MaterializedView'",
        {"dbs": cell_scan.databases()},
    )
    assert views
    edges = []
    for view in views:
        to = re.search(r"\bTO (\w+)\.(\w+)", str(view["create_table_query"]))
        assert to, view
        sources = re.findall(r"\b(?:FROM|JOIN)\s+(\w+)\.(\w+)", str(view["as_select"]))
        edges.append(
            (
                {f"{ch_names.logical(db)}.{name}" for db, name in sources},
                f"{ch_names.logical(to.group(1))}.{to.group(2)}",
            )
        )
    assert closure(_LOGGED, edges) == cell_scan.LOG_TABLES


async def test_the_schemas_own_objects_read_nothing_outside(ch: Any) -> None:
    """Every view, materialized view and table of the schema, judged as the writer check
    judges objects inside the configured databases: none reads outside them."""
    rows = await ch.query_all(
        "SELECT database, name, engine, engine_full, create_table_query, "
        "dependencies_database, dependencies_table FROM system.tables "
        "WHERE database IN {dbs:Array(String)}",
        {"dbs": cell_scan.databases()},
    )
    assert any(r["engine"] == "View" for r in rows) and any(
        r["engine"] == "MaterializedView" for r in rows
    )
    functions = frozenset(
        str(r["name"]).lower()
        for r in await ch.query_all("SELECT name FROM system.table_functions")
    )
    assert cell_writers.object_problems(rows, cell_scan.databases(), functions) == {}
