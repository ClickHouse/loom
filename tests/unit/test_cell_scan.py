"""`server/cli/cell_scan.py`: the table list, its judging against the erasure classification
and the hosts of a cluster, and the org scan, on a fake ClickHouse. A cluster of several
hosts cannot run in CI, so its paths are pinned here: the per-host listing and its retry,
the `ON CLUSTER` statements' settings and statuses, and the host row each scan statement
returns. CH-free."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.cli import cell_scan
from server.cli.cell_scan import ScanResult, Table, TableList
from server.core import cell, cell_ch, pg
from server.core.cell_ch import Refused, Topology
from server.core.cell_sync import Budget
from server.insights.store import cell_catalog

ONE = Topology("default", 1, (("localhost", 9000),))
TWO = Topology("main", 1, (("ch-0", 9000), ("ch-1", 9000)))
NAMES = {"os-0": "ch-0", "os-1": "ch-1"}

Answer = list[dict[str, Any]] | Callable[[str, dict[str, Any]], list[dict[str, Any]]]


class _DatabaseError(Exception):
    """A server error as the driver words one: `Code: <n>.` first."""


@dataclass
class FakeCH:
    """Answers reads by the first route whose needle the SQL holds; records every call with
    its settings. A route answering an exception raises it."""

    routes: list[tuple[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def on(self, needle: str, answer: Any) -> FakeCH:
        self.routes.append((needle, answer))
        return self

    def _answer(self, kind: str, sql: str, params: dict[str, Any] | None, settings: Any) -> Any:
        self.calls.append((kind, sql, dict(settings or {})))
        for i, (needle, answer) in enumerate(self.routes):
            if needle in sql:
                if isinstance(answer, list) and answer and isinstance(answer[0], Exception):
                    # A sequence of answers: each exception is raised once.
                    self.routes[i] = (needle, answer[1:] if len(answer) > 2 else answer[1])
                    raise answer[0]
                if isinstance(answer, Exception):
                    raise answer
                return answer(sql, params or {}) if callable(answer) else answer
        raise AssertionError(f"unrouted {kind}: {sql[:200]}")

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return [dict(r) for r in self._answer("query_all", sql, params, None)]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Any = None, *, settings: Any = None) -> None:
        self._answer("execute", sql, params, settings)

    async def raw_bytes(self, sql: str, params: Any = None, *, settings: Any = None) -> bytes:
        rows = self._answer("raw_bytes", sql, params, settings)
        return "".join(json.dumps(r) + "\n" for r in rows).encode()

    @asynccontextmanager
    async def raw_stream(
        self, sql: str, params: Any = None, *, settings: Any = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        rows = self._answer("raw_stream", sql, params, settings)

        async def chunks() -> AsyncIterator[bytes]:
            # Each row split across two chunks: a reader that assumes whole lines fails.
            for r in rows:
                line = (json.dumps(r) + "\n").encode()
                yield line[:3]
                yield line[3:]

        yield chunks()

    def sql(self, kind: str) -> list[str]:
        return [s for k, s, _ in self.calls if k == kind]


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(_: float) -> None:
        return None

    monkeypatch.setattr(cell_scan, "_sleep", _none)


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_the_budget_counts_down_and_refuses_naming_the_subject() -> None:
    clock = _Clock()
    budget = Budget(60, clock)
    clock.t += 45
    assert budget.left() == 15 and budget.elapsed() == 45
    assert budget.require("clickhouse loom.memory") == 15
    clock.t += 15
    with pytest.raises(Refused, match=r"clickhouse loom.memory: the scan ran out of --scan-budget"):
        budget.require("clickhouse loom.memory")


# -- the cluster -------------------------------------------------------------------------


async def test_the_topology_reads_the_configured_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cell_ch.settings, "ch_cluster", "main")
    ch = FakeCH().on(
        "FROM system.clusters",
        [
            {"host_name": "ch-0", "port": 9000, "shard_num": 1},
            {"host_name": "ch-1", "port": 9000, "shard_num": 2},
        ],
    )
    topology = await cell_ch.read_topology(ch)
    assert topology == Topology("main", 2, (("ch-0", 9000), ("ch-1", 9000)))
    assert topology.multi
    assert "2 shards" in str(topology.refusals()[0])
    monkeypatch.setattr(cell_ch.settings, "ch_cluster", "")
    assert await cell_ch.read_topology(FakeCH()) == Topology("", 0)


async def test_two_servers_under_one_host_name_are_two_and_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell_ch.settings, "ch_cluster", "main")
    ch = FakeCH().on(
        "FROM system.clusters",
        [
            {"host_name": "ch-0", "port": 9000, "shard_num": 1},
            {"host_name": "ch-0", "port": 9001, "shard_num": 1},
        ],
    )
    topology = await cell_ch.read_topology(ch)
    # Several servers: a plain MergeTree table there refuses, statements carry ON CLUSTER.
    assert topology.multi and topology.hosts == frozenset({"ch-0"})
    assert [str(r) for r in topology.refusals()] == [
        "cluster main: lists host ch-0 2 times (ports 9000, 9001): give each server its own "
        "host name"
    ]
    _, refusals = cell_scan.judge_clickhouse(
        [Table("loom", "memory", "MergeTree")], {"loom.memory"}, topology
    )
    assert "would miss the others' rows" in refusals[0].reason


# Attach's own grants on a cluster, as its `SHOW GRANTS FINAL` prints them.
_FULL = [
    {"g": "GRANT SELECT, SHOW, SYSTEM, CLUSTER, REMOTE ON *.* TO attach"},
    {"g": "GRANT SHOW USERS, SHOW ROLES, SHOW ROW POLICIES ON *.* TO attach"},
]


def _granted(ch: FakeCH, grants: list[dict[str, str]] = _FULL) -> FakeCH:
    return ch.on("currentUser()", [{"u": "attach"}]).on("SHOW GRANTS FINAL", grants)


async def test_each_host_answers_for_exactly_one_listed_name() -> None:
    ch = _granted(
        FakeCH().on(
            "clusterAllReplicas",
            [{"h": "os-0", "names": ["ch-0"]}, {"h": "os-1", "names": ["ch-1"]}],
        )
    )
    assert await cell_catalog.host_names(ch, TWO) == NAMES
    # Every clusterAllReplicas read waits for every replica and reads its own host locally.
    reads = [s for s in ch.sql("query_all") if "clusterAllReplicas" in s]
    assert "skip_unavailable_shards = 0, prefer_localhost_replica = 1" in reads[0]


async def test_a_user_without_read_on_remote_is_named_before_any_cluster_read() -> None:
    narrow = [g for g in _FULL if "REMOTE" not in g["g"]] + [
        {"g": "GRANT SELECT, SHOW, SYSTEM, CLUSTER ON *.* TO attach"}
    ]
    ch = _granted(FakeCH(), narrow)
    with pytest.raises(Refused, match="attach's own user lacks READ ON REMOTE"):
        await cell_catalog.host_names(ch, TWO)
    assert not [s for s in ch.sql("query_all") if "clusterAllReplicas" in s]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ([{"h": "os-0", "names": ["ch-0"]}, {"h": "os-1", "names": []}], "local interface"),
        # Two local entries, or two servers with one OS hostname, fall in one group.
        ([{"h": "os-0", "names": ["ch-0", "ch-1"]}], "several entries"),
        ([{"h": "os-0", "names": ["ch-0"]}], "no answer for ch-1"),
        ([{"h": "os-0", "names": ["ch-0"]}, {"h": "os-1", "names": ["ch-0"]}], "not the listed"),
    ],
)
async def test_a_host_without_one_configured_name_refuses(
    rows: list[dict[str, Any]], reason: str
) -> None:
    with pytest.raises(Refused, match=reason):
        await cell_catalog.host_names(_granted(FakeCH().on("clusterAllReplicas", rows)), TWO)


# -- the table list ----------------------------------------------------------------------


def _listing(*per_host: tuple[str, list[tuple[str, str]]]) -> FakeCH:
    """Each host's `org` tables of database loom, as (name, engine)."""
    columns = [{"h": h, "database": "loom", "table": n} for h, ts in per_host for n, _ in ts]
    tables = [
        {"h": h, "database": "loom", "name": n, "engine": e} for h, ts in per_host for n, e in ts
    ]
    return FakeCH().on("system.columns", columns).on("system.tables", tables)


async def test_a_table_one_replica_lists_alone_refuses_naming_host_and_table() -> None:
    ch = _listing(
        ("os-0", [("memory", "ReplicatedReplacingMergeTree"), ("stray", "ReplicatedMergeTree")]),
        ("os-1", [("memory", "ReplicatedReplacingMergeTree")]),
    )
    tables, refusals = await cell_scan.list_clickhouse(ch, TWO, NAMES, ["loom"])
    assert tables == []
    assert [str(r) for r in refusals] == [
        "host ch-1: does not list loom.stray, which another lists"
    ]
    # Listed twice: the disagreement may have been a refresh in flight.
    assert len(ch.sql("query_all")) == 4
    assert all("clusterAllReplicas({c:String}, system." in s for s in ch.sql("query_all"))


async def test_an_exchange_table_is_skipped_and_the_listing_read_again() -> None:
    first = [
        ("memory", "ReplicatedReplacingMergeTree"),
        (".tmp.inner_id.0b6b0e0b-1d1f-4a2e-9a55-0d8a3b1c7e11", "ReplicatedAggregatingMergeTree"),
    ]
    ch = _listing(("os-0", first), ("os-1", [("memory", "ReplicatedReplacingMergeTree")]))
    tables, refusals = await cell_scan.list_clickhouse(ch, TWO, NAMES, ["loom"])
    assert refusals == []
    assert tables == [Table("loom", "memory", "ReplicatedReplacingMergeTree")]
    assert len(ch.sql("query_all")) == 4


async def test_hosts_that_disagree_on_an_engine_refuse() -> None:
    ch = _listing(("os-0", [("memory", "ReplicatedMergeTree")]), ("os-1", [("memory", "Memory")]))
    _, refusals = await cell_scan.list_clickhouse(ch, TWO, NAMES, ["loom"])
    assert {r.subject for r in refusals} == {"host ch-0", "host ch-1"}


async def test_one_host_reads_the_system_tables_themselves() -> None:
    ch = _listing(("h", [("memory", "ReplacingMergeTree")]))
    tables, refusals = await cell_scan.list_clickhouse(ch, ONE, {}, ["loom"])
    assert refusals == [] and tables == [Table("loom", "memory", "ReplacingMergeTree")]
    assert not any("clusterAllReplicas" in s for s in ch.sql("query_all"))


_CLASSIFIED = {"loom.memory", "loom.agent_events", "loom.graph_projections", "loom.memory_v"}


def test_engines_are_judged_and_log_tables_left_unscanned() -> None:
    tables = [
        Table("loom", "memory", "ReplacingMergeTree"),
        Table("loom", "agent_events", "MergeTree"),
        Table("loom", "graph_projections", "KeeperMap"),
        Table("loom", "memory_v", "View"),
        Table("loom", "memory_mv", "MaterializedView"),
        Table("loom", "memory_buffer", "Buffer"),
        Table("loom", "brand_new", "MergeTree"),
        Table("loom", "odd", ""),
    ]
    scanned, refusals = cell_scan.judge_clickhouse(tables, _CLASSIFIED, ONE)
    assert [t.name for t in scanned] == ["memory", "graph_projections"]
    assert {str(r) for r in refusals} == {
        "clickhouse loom.memory_buffer: engine Buffer holds an org column the scan cannot read",
        "clickhouse loom.brand_new: missing from erasure's table classification",
        "clickhouse loom.odd: engine unknown holds an org column the scan cannot read",
    }


def test_the_classification_compares_canonical_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cell_ch.settings, "ch_database", "prod_loom")
    monkeypatch.setattr(cell_ch.settings, "ch_telemetry_database", "prod_telemetry")
    tables = [
        Table("prod_loom", "memory", "MergeTree"),
        Table("prod_telemetry", "sli_minute", "MergeTree"),
    ]
    scanned, refusals = cell_scan.judge_clickhouse(
        tables, {"loom.memory", "loom_telemetry.sli_minute"}, ONE
    )
    assert refusals == [] and [t.canonical for t in scanned] == ["loom.memory"]
    assert scanned[0].sql == "`prod_loom`.`memory`"


def test_a_cluster_refuses_tables_one_host_cannot_scan_alone() -> None:
    tables = [
        Table("loom", "memory", "MergeTree"),
        Table("loom", "graph_projections", "KeeperMap"),
        Table("loom", "agent_events", "SharedMergeTree"),
    ]
    _, refusals = cell_scan.judge_clickhouse(tables, _CLASSIFIED, TWO)
    assert [r.subject for r in refusals] == ["clickhouse loom.memory"]
    assert "would miss the others' rows" in refusals[0].reason
    # A replicated table needs a listed cluster to sync through, even on one host.
    replicated = [Table("loom", "memory", "ReplicatedMergeTree")]
    _, refusals = cell_scan.judge_clickhouse(replicated, _CLASSIFIED, Topology("", 0))
    assert "LOOM_CH_CLUSTER (unset) lists no host" in refusals[0].reason
    scanned, refusals = cell_scan.judge_clickhouse(replicated, _CLASSIFIED, ONE)
    assert refusals == [] and scanned == replicated
    # So does a shared one: an unlisted cluster is not known to be one host.
    shared = [Table("loom", "memory", "SharedReplacingMergeTree")]
    unlisted = Topology("main", 0)
    _, refusals = cell_scan.judge_clickhouse(shared, _CLASSIFIED, unlisted)
    assert "LOOM_CH_CLUSTER (main) lists no host" in refusals[0].reason


def test_a_provider_transition_refuses_up_front() -> None:
    copy = Table("loom", "_provider_rebuild_" + "0" * 32 + "_memory_embeddings_target", "MergeTree")
    assert [r.reason for r in cell_scan.provider_copies([copy])] == [
        "a provider transition is running"
    ]
    assert cell_scan.provider_copies([Table("loom", "memory", "MergeTree")]) == []


async def test_a_classified_table_the_listing_missed_is_described() -> None:
    ch = (
        FakeCH()
        .on("`loom`.`hidden`", [{"name": "org"}, {"name": "id"}])
        .on("`loom`.`no_org`", [{"name": "id"}])
        .on(
            "`loom`.`gone`",
            _DatabaseError("Code: 60. DB::Exception: Table loom.gone does not exist"),
        )
        .on("`loom`.`denied`", _DatabaseError("Code: 497. DB::Exception: Not enough privileges"))
    )
    listed = [Table("loom", "memory", "MergeTree")]
    classified = {"loom.memory", "loom.hidden", "loom.no_org", "loom.gone", "loom.denied"}
    refusals = await cell_scan.reverse_clickhouse(ch, listed, classified)
    assert [r.subject for r in refusals] == ["clickhouse loom.denied", "clickhouse loom.hidden"]
    assert "DESCRIBE TABLE failed: Code: 497" in refusals[0].reason
    assert "did not return" in refusals[1].reason
    assert all(s.startswith("DESCRIBE TABLE `loom`.") for s in ch.sql("query_all"))


# -- the orgs ----------------------------------------------------------------------------


def test_each_org_is_judged_by_the_routers_rule() -> None:
    result = ScanResult()
    long = "a" * (cell.ORG_MAX_BYTES + 1)
    for value in [b"acme", "acme", b"", b"-", "*", b"\xff\xfe", "a\x00b", None, long, "é" * 1024]:
        result.add("clickhouse loom.memory", value)
    assert result.orgs == {
        "acme": {"clickhouse loom.memory"},
        "é" * 1024: {"clickhouse loom.memory"},
    }
    assert result.malformed == {"clickhouse loom.memory": ["fffe", "'a\\x00b'", "NULL"]}
    assert result.too_long == {long: {"clickhouse loom.memory"}}


def test_the_configured_orgs_are_read_from_both_fingerprint_values() -> None:
    current = {"v": 1, "fields": {"configured_orgs": ["acme", "beta"]}}
    nxt = {"v": 1, "fields": {"configured_orgs": ["gamma"]}}
    assert cell_scan.configured_orgs(current, nxt, None) == {"acme", "beta", "gamma"}


def _org_rows(*orgs: bytes) -> list[dict[str, Any]]:
    return [{"k": "o", "v": o.hex().upper()} for o in orgs]


async def test_one_host_scans_without_on_cluster() -> None:
    ch = FakeCH().on("SYSTEM", []).on("SELECT DISTINCT org", _org_rows(b"acme", b"\xff"))
    tables = TableList(clickhouse=[Table("loom", "memory", "ReplicatedMergeTree")])
    result = await cell_scan.scan(ch, tables, ONE, Budget(7200), configured=["beta"])
    assert result.orgs == {"acme": {"clickhouse loom.memory"}, "beta": {"configured"}}
    assert result.malformed == {"clickhouse loom.memory": ["ff"]}
    assert ch.sql("execute") == [
        "SYSTEM FLUSH ASYNC INSERT QUEUE",
        "SYSTEM SYNC REPLICA `loom`.`memory` LIGHTWEIGHT",
    ]
    (scan_sql,) = ch.sql("raw_stream")
    assert scan_sql == (
        "SELECT 'o' AS k, hex(org) AS v FROM (SELECT DISTINCT org FROM `loom`.`memory`) "
        "FORMAT JSONEachRow"
    )
    settings = [s for k, _, s in ch.calls if k == "raw_stream"][0]
    assert 7190 <= settings["max_execution_time"] <= 7200
    assert all(
        s["receive_timeout"] == s["max_execution_time"] for k, _, s in ch.calls if k == "execute"
    )


def _cluster_status(*hosts: str, status: int = 0) -> list[dict[str, Any]]:
    return [
        # A 64-bit status, which JSONEachRow quotes, as a server sends it.
        {"host": h, "port": 9000, "status": str(status), "error": "" if not status else "lost"}
        for h in hosts
    ]


async def test_a_cluster_syncs_every_replica_and_reads_the_scans_host() -> None:
    rows = [{"k": "h", "v": '["ch-1"]'}, *_org_rows(b"acme")]
    ch = FakeCH().on("SYSTEM", _cluster_status("ch-0", "ch-1")).on("SELECT DISTINCT org", rows)
    tables = TableList(clickhouse=[Table("loom", "memory", "ReplicatedMergeTree")])
    result = await cell_scan.scan(ch, tables, TWO, Budget(600))
    assert result.orgs == {"acme": {"clickhouse loom.memory"}}
    assert ch.sql("raw_bytes") == [
        "SYSTEM FLUSH ASYNC INSERT QUEUE ON CLUSTER `main`",
        "SYSTEM SYNC REPLICA ON CLUSTER `main` `loom`.`memory` LIGHTWEIGHT",
    ]
    for kind, _, settings in ch.calls:
        if kind == "raw_bytes":
            # Query-level settings: SYSTEM … ON CLUSTER takes no SETTINGS clause.
            assert settings["distributed_ddl_output_mode"] == "throw"
            assert settings["distributed_ddl_task_timeout"] == settings["receive_timeout"]
    assert "WHERE cluster = {c:String} AND is_local" in ch.sql("raw_stream")[0]


async def test_shared_merge_tree_reads_sequentially_consistent() -> None:
    ch = FakeCH().on("SYSTEM", []).on("SELECT DISTINCT org", [])
    tables = TableList(clickhouse=[Table("loom", "memory", "SharedReplacingMergeTree")])
    await cell_scan.scan(ch, tables, ONE, Budget(600))
    assert ch.sql("execute") == ["SYSTEM FLUSH ASYNC INSERT QUEUE"]
    settings = [s for k, _, s in ch.calls if k == "raw_stream"][0]
    assert settings["select_sequential_consistency"] == 1


@pytest.mark.parametrize(
    ("status", "rows", "reason"),
    [
        (
            _cluster_status("ch-0", "ch-1", status=1),
            [],
            "replica ch-0: clickhouse loom.memory: lost",
        ),
        (_cluster_status("ch-0"), [], r"not the listed ones \(missing: ch-1\)"),
        (_cluster_status("ch-0", "ch-1"), [], "named itself nothing"),
        (_cluster_status("ch-0", "ch-1"), [{"k": "h", "v": '["ch-9"]'}], "named itself"),
        (_cluster_status("ch-0", "ch-1"), [{"k": "h", "v": '["ch-0", "ch-1"]'}], "named itself"),
    ],
)
async def test_a_cluster_statement_that_cannot_vouch_for_every_replica_refuses(
    status: list[dict[str, Any]], rows: list[dict[str, Any]], reason: str
) -> None:
    ch = FakeCH().on("SYSTEM", status).on("SELECT DISTINCT org", rows)
    tables = TableList(clickhouse=[Table("loom", "memory", "ReplicatedMergeTree")])
    with pytest.raises(Refused, match=reason):
        await cell_scan.scan(ch, tables, TWO, Budget(600))


async def test_an_empty_table_on_a_cluster_still_names_its_host() -> None:
    ch = FakeCH().on("SYSTEM", _cluster_status("ch-0", "ch-1"))
    ch.on("SELECT DISTINCT org", [{"k": "h", "v": '["ch-0"]'}])
    tables = TableList(clickhouse=[Table("loom", "graph_projections", "KeeperMap")])
    result = await cell_scan.scan(ch, tables, TWO, Budget(600))
    assert result.orgs == {} and result.malformed == {}


async def test_a_transient_failure_is_retried_and_a_timeout_refuses() -> None:
    from clickhouse_connect.driver.exceptions import OperationalError

    tables = TableList(clickhouse=[Table("loom", "memory", "MergeTree")])
    ch = (
        FakeCH()
        .on("SYSTEM", [])
        .on("SELECT DISTINCT org", [OperationalError("Network Error: reset"), _org_rows(b"acme")])
    )
    result = await cell_scan.scan(ch, tables, ONE, Budget(600))
    assert result.orgs == {"acme": {"clickhouse loom.memory"}}
    assert len(ch.sql("raw_stream")) == 2
    timeout = _DatabaseError("Code: 159. DB::Exception: Timeout exceeded: elapsed 600 seconds")
    ch = FakeCH().on("SYSTEM", []).on("SELECT DISTINCT org", timeout)
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan ran out of --scan-budget"):
        await cell_scan.scan(ch, tables, ONE, Budget(1))  # the budget is spent
    # With budget left, a timeout is the server's own (a profile's estimate): named.
    ch = FakeCH().on("SYSTEM", []).on("SELECT DISTINCT org", timeout)
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan failed: Code: 159"):
        await cell_scan.scan(ch, tables, ONE, Budget(600))
    ch = FakeCH().on("SYSTEM", _DatabaseError("Code: 497. DB::Exception: Not enough privileges"))
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan failed: Code: 497"):
        await cell_scan.scan(ch, tables, ONE, Budget(600))


async def test_a_spent_budget_refuses_before_any_statement() -> None:
    clock = _Clock()
    budget = Budget(10, clock)
    clock.t += 10
    ch = FakeCH()
    with pytest.raises(Refused, match="ran out of --scan-budget"):
        await cell_scan.scan(
            ch, TableList(clickhouse=[Table("loom", "memory", "MergeTree")]), ONE, budget
        )
    assert ch.calls == []


async def test_a_listing_with_refusals_is_never_scanned() -> None:
    tables = TableList(refusals=[cell_ch.Refusal("clickhouse loom.x", "engine Memory")])
    with pytest.raises(Refused, match="engine Memory"):
        await cell_scan.scan(FakeCH(), tables, ONE, Budget(600))


def test_the_postgres_skip_scan_quotes_its_table() -> None:
    sql = cell_scan.pg_skip_scan_sql('loom.odd"name')
    assert 'FROM "loom"."odd""name" WHERE org IS NOT NULL ORDER BY org LIMIT 1' in sql
    assert "t.org > o.org ORDER BY t.org LIMIT 1" in sql


class _PgOutage(OSError):
    """A dropped connection: `pg.is_outage` reads an OSError as one."""


class _Canceled(Exception):
    sqlstate = "57014"  # query_canceled: the statement timeout fired


def _pg_sessions(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> list[int]:
    """`pg.dedicated` answering one session per entry: an exception raised on entering it,
    or the rows of the skip scan. Records each session's statement timeout."""
    from server.core import pg

    timeouts: list[int] = []
    queue = list(answers)

    class _Conn:
        def __init__(self, rows: list[dict[str, str]] | None) -> None:
            self.rows = rows

        async def fetchval(self, sql: str, *_: Any) -> bool:
            return True  # the org index exists

        async def fetch(self, sql: str) -> list[dict[str, str]]:
            if self.rows is None:
                await asyncio.Event().wait()  # a primary that vanished: no answer, ever
            return self.rows or []

    @asynccontextmanager
    async def dedicated(*, statement_timeout_ms: int = 0, marked: bool = True) -> Any:
        timeouts.append(statement_timeout_ms)
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        yield _Conn(answer)

    monkeypatch.setattr(pg, "dedicated", dedicated)
    return timeouts


@pytest.mark.parametrize(
    "failure",
    [_PgOutage("connection reset"), pg.PostgresUnavailableError("Postgres did not answer")],
)
async def test_the_postgres_scan_retries_an_outage_once_it_clears(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    timeouts = _pg_sessions(monkeypatch, failure, [{"org": "acme"}])
    tables = TableList(postgres=["loom.tenancy"])
    result = await cell_scan.scan(FakeCH(), tables, ONE, Budget(60))
    assert result.orgs == {"acme": {"postgres loom.tenancy"}}
    assert len(timeouts) == 2 and all(0 < t <= 60_000 for t in timeouts)


async def test_the_postgres_statement_timeout_refuses_as_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The statement timeout is what was left of the budget: a cancel with none left is it,
    and one with budget left is a cancel from outside, retried as an outage."""
    _pg_sessions(monkeypatch, _Canceled("canceling statement due to statement timeout"))
    with pytest.raises(Refused, match="postgres loom.tenancy: the scan ran out of --scan-budget"):
        await cell_scan.scan(FakeCH(), TableList(postgres=["loom.tenancy"]), ONE, Budget(1))
    _pg_sessions(monkeypatch, _Canceled("canceling statement due to user request"), [{"org": "a"}])
    result = await cell_scan.scan(FakeCH(), TableList(postgres=["loom.tenancy"]), ONE, Budget(60))
    assert result.orgs == {"a": {"postgres loom.tenancy"}}


async def test_the_listing_refuses_a_user_that_would_list_short() -> None:
    ch = (
        FakeCH()
        .on("currentUser()", [{"u": "attach"}])
        .on("SHOW GRANTS FINAL", [{"g": "GRANT SELECT ON loom.memory TO attach"}])
    )
    listed = await cell_scan.list_tables(ch, None, ONE, {})
    assert [r.subject for r in listed.refusals] == ["user attach"]
    assert "SHOW TABLES ON *.*" in listed.refusals[0].reason
    assert listed.clickhouse == [] and listed.postgres == []
    assert not [sql for _, sql, _ in ch.calls if "system." in sql]


class _RawBody(FakeCH):
    """Streams a scan's body as raw text, as ClickHouse writes it: each scan the next of
    `bodies`, the last one again once they run out."""

    def __init__(self, *bodies: str) -> None:
        super().__init__()
        self.bodies = list(bodies)
        self.on("SYSTEM", [])

    @asynccontextmanager
    async def raw_stream(
        self, sql: str, params: Any = None, *, settings: Any = None
    ) -> AsyncIterator[AsyncIterator[bytes]]:
        self.calls.append(("raw_stream", sql, dict(settings or {})))
        data = (self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]).encode()

        async def chunks() -> AsyncIterator[bytes]:
            yield data[:7]
            yield data[7:]

        yield chunks()


_ROWS = '{"k":"o","v":"61636D65"}\n{"k":"o","v":"62657461"}\n'
# What a server writes once a statement fails after its first block: a text tail...
_TEXT_TAIL = (
    "\n__exception__\nsryzxwaxggultnva\nCode: 159. DB::Exception: Timeout exceeded: elapsed "
    "1005.795 ms, maximum: 1000.000 ms. (TIMEOUT_EXCEEDED) (version 26.10.3.7 (official "
    "build))\n143 sryzxwaxggultnva\n__exception__\n"
)
# ...or, with `http_write_exception_in_output_format`, a row of the format.
_JSON_TAIL = '{"exception": "Code: 159. DB::Exception: Timeout exceeded (TIMEOUT_EXCEEDED)"}\n'


@pytest.mark.parametrize("tail", [_TEXT_TAIL, _JSON_TAIL])
async def test_a_timeout_written_into_the_body_mid_stream_refuses_as_the_budget(
    tail: str,
) -> None:
    ch = _RawBody(_ROWS + tail)
    tables = TableList(clickhouse=[Table("loom", "memory", "MergeTree")])
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan ran out of --scan-budget"):
        await cell_scan.scan(ch, tables, ONE, Budget(1))  # the budget is spent


async def test_any_other_error_in_the_body_is_named() -> None:
    ch = _RawBody(_ROWS + '{"exception": "Code: 241. DB::Exception: Memory limit exceeded"}\n')
    tables = TableList(clickhouse=[Table("loom", "memory", "MergeTree")])
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan failed: Code: 241"):
        await cell_scan.scan(ch, tables, ONE, Budget(600))
    ch = _RawBody('{"unexpected": 1}\n')
    with pytest.raises(Refused, match="the scan failed"):
        await cell_scan.scan(ch, tables, ONE, Budget(600))


async def test_an_on_cluster_row_without_a_status_is_read_as_its_error() -> None:
    # A Keeper session lost (999) clears by itself: retried, as when it is raised.
    flaky = [{"exception": "Code: 999. DB::Exception: Keeper session expired"}]
    answers = iter([flaky, _cluster_status("ch-0", "ch-1"), _cluster_status("ch-0", "ch-1")])
    ch = FakeCH().on("SYSTEM", lambda *_: next(answers))
    ch.on("SELECT DISTINCT org", [{"k": "h", "v": '["ch-0"]'}, *_org_rows(b"acme")])
    tables = TableList(clickhouse=[Table("loom", "memory", "ReplicatedMergeTree")])
    result = await cell_scan.scan(ch, tables, TWO, Budget(600))
    assert result.orgs == {"acme": {"clickhouse loom.memory"}}
    assert len(ch.sql("raw_bytes")) == 3
    # Any other is named.
    ch = FakeCH().on("SYSTEM", [{"host": "ch-0", "error": "no status"}])
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan failed"):
        await cell_scan.scan(ch, tables, TWO, Budget(600))


async def test_a_transient_error_in_the_scan_body_is_retried() -> None:
    readonly = '{"exception": "Code: 242. DB::Exception: Table is in readonly mode"}\n'
    ch = _RawBody(_ROWS + readonly, _ROWS)
    tables = TableList(clickhouse=[Table("loom", "memory", "MergeTree")])
    result = await cell_scan.scan(ch, tables, ONE, Budget(600))
    assert set(result.orgs) == {"acme", "beta"}
    assert len(ch.sql("raw_stream")) == 2


async def test_every_read_throws_past_a_limit_rather_than_stopping_short() -> None:
    ch = FakeCH().on("SYSTEM", []).on("SELECT DISTINCT org", _org_rows(b"acme"))
    tables = TableList(clickhouse=[Table("loom", "memory", "MergeTree")])
    await cell_scan.scan(ch, tables, ONE, Budget(600))
    settings = [s for k, _, s in ch.calls if k == "raw_stream"][0]
    modes = [k for k in cell_ch.WHOLE if "overflow_mode" in k]
    assert "group_by_overflow_mode" in modes and len(modes) == 8
    for name in modes:
        assert settings[name] == "throw", name
    # Nor does a setting cut, filter or sample the rows.
    for name, value in (
        ("limit", "0"),
        ("offset", "0"),
        ("additional_table_filters", "{}"),
        ("additional_result_filter", ""),
        ("final", "0"),
        ("enable_parallel_replicas", "0"),
        ("parallel_replicas_count", "0"),
        ("parallel_replica_offset", "0"),
        ("use_query_cache", "0"),
    ):
        assert settings[name] == value, name
    # The listing's reads carry the same, on one host and through clusterAllReplicas.
    for topology, names in ((ONE, {}), (TWO, NAMES)):
        ch = _listing(("os-0", []), ("os-1", []))
        await cell_scan.list_clickhouse(ch, topology, names, ["loom"])
        for sql in ch.sql("query_all"):
            assert "distinct_overflow_mode = 'throw'" in sql and "limit = '0'" in sql, sql
            assert "additional_table_filters = '{}'" in sql and "offset = '0'" in sql, sql


async def test_a_postgres_server_that_never_answers_refuses_as_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cell_scan, "_PG_MARGIN_S", 0.05)
    _pg_sessions(monkeypatch, None)
    with pytest.raises(Refused, match="postgres loom.tenancy: the scan ran out of --scan-budget"):
        await cell_scan.scan(FakeCH(), TableList(postgres=["loom.tenancy"]), ONE, Budget(0.05))


async def test_a_sync_that_does_not_finish_names_its_hosts_not_the_budget() -> None:
    timeout = "Code: 159. DB::Exception: Distributed DDL task is not finished on 1 of 2 hosts"
    ch = FakeCH().on(
        "SYSTEM SYNC REPLICA",
        [{"host": "ch-0", "port": 9000, "status": "0", "error": ""}, {"exception": timeout}],
    )
    ch.on("SYSTEM FLUSH", _cluster_status("ch-0", "ch-1"))
    tables = TableList(clickhouse=[Table("loom", "memory", "ReplicatedMergeTree")])
    with pytest.raises(Refused) as refused:
        await cell_scan.scan(ch, tables, TWO, Budget(600))
    (refusal,) = refused.value.refusals
    assert refusal.reason.startswith("SYSTEM SYNC REPLICA did not finish on ch-1: Code: 159.")
    assert "--scan-budget" not in refusal.reason
    # On one host, that host.
    ch = FakeCH().on("SYSTEM", _DatabaseError("Code: 159. DB::Exception: Timeout exceeded"))
    with pytest.raises(
        Refused, match="SYSTEM FLUSH ASYNC INSERT QUEUE did not finish on localhost"
    ):
        await cell_scan.scan(ch, tables, ONE, Budget(600))


async def test_two_servers_under_one_host_name_refuse_before_any_read() -> None:
    same = Topology("main", 1, (("ch-0", 9000), ("ch-0", 9001)))
    ch = FakeCH()
    with pytest.raises(Refused, match="lists host ch-0 2 times"):
        await cell_catalog.host_names(ch, same)
    listed = await cell_scan.list_tables(ch, None, same, {})
    assert [r.reason for r in listed.refusals] == [
        "lists host ch-0 2 times (ports 9000, 9001): give each server its own host name"
    ]
    assert ch.calls == []


async def test_a_sync_cut_by_the_budget_refuses_as_the_budget() -> None:
    clock = _Clock()
    budget = Budget(60, clock)
    timeout = _DatabaseError("Code: 159. DB::Exception: Timeout exceeded")

    def spend(*_: Any) -> list[dict[str, Any]]:
        clock.t += 61  # the statement ran the budget out
        raise timeout

    ch = FakeCH().on("SYSTEM", spend)
    tables = TableList(clickhouse=[Table("loom", "memory", "ReplicatedMergeTree")])
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan ran out of --scan-budget"):
        await cell_scan.scan(ch, tables, ONE, budget)
    # Given the budget's whole seconds, the statement times out with under one left.
    clock, spent = _Clock(), 100.7
    budget = Budget(spent, clock)

    def spend_whole(*_: Any) -> list[dict[str, Any]]:
        clock.t += 100  # the 100 s it was given: 0.7 s is left
        raise timeout

    ch = FakeCH().on("SYSTEM", spend_whole)
    with pytest.raises(Refused, match="clickhouse loom.memory: the scan ran out of --scan-budget"):
        await cell_scan.scan(ch, tables, ONE, budget)


async def test_a_detached_table_refuses_on_any_host() -> None:
    rows = [
        {"h": "os-1", "database": "loom", "table": "memory", "is_permanently": 0},
        {"h": "os-0", "database": "loom_telemetry", "table": "sli_minute", "is_permanently": 1},
    ]
    ch = FakeCH().on("system.detached_tables", rows)
    refusals = await cell_scan.detached_tables(ch, TWO, NAMES, ["loom", "loom_telemetry"])
    assert [str(r) for r in refusals] == [
        "clickhouse loom_telemetry.sli_minute: a permanently detached table on host ch-0: "
        "attach or drop it before the scan",
        "clickhouse loom.memory: a detached table on host ch-1: attach or drop it before the scan",
    ]
    (sql,) = ch.sql("query_all")
    assert "clusterAllReplicas({c:String}, system.detached_tables)" in sql
    one = FakeCH().on("system.detached_tables", [{"h": "x", **rows[0]}])
    assert [r.reason for r in await cell_scan.detached_tables(one, ONE, {}, ["loom"])] == [
        "a detached table: attach or drop it before the scan"
    ]


class _PgCatalog:
    """The listing's reads of a Postgres catalogue: `listed` tables with an `org` column,
    `hidden` ones the listing leaves out though `pg_attribute` shows one."""

    def __init__(self, listed: list[str], hidden: set[str]) -> None:
        self.listed, self.hidden = listed, hidden

    async def fetch(self, sql: str) -> list[dict[str, str]]:
        return [{"relname": n.removeprefix("loom."), "relkind": "r"} for n in self.listed]

    async def fetchval(self, sql: str, name: str) -> bool:
        return name in self.hidden


async def test_a_classified_postgres_table_the_listing_missed_refuses() -> None:
    from server.erasure.store.steps_pg import PG_NO_MEMORY_CONTENT

    assert "loom.tenancy" in PG_NO_MEMORY_CONTENT  # no memory content, and every org
    names, refusals = await cell_scan.list_postgres(
        _PgCatalog(["loom.memory_current"], {"loom.tenancy"})
    )
    assert names == ["loom.memory_current"]
    assert [str(r) for r in refusals] == [
        "postgres loom.tenancy: has an org column the table listing did not return"
    ]
