"""`server/cli/cell_scan.py`: the table list, its judging against the erasure classification
and the hosts of a cluster, and the org scan, on a fake ClickHouse. A cluster of several
hosts cannot run in CI, so its paths are pinned here: the per-host listing and its retry,
the `ON CLUSTER` statements' settings and statuses, and the host row each scan statement
returns. CH-free."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.cli import cell_scan
from server.cli.cell_scan import Budget, Refused, ScanResult, Table, TableList, Topology
from server.core import cell
from server.core.errors import BackendUnavailableError

ONE = Topology("default", frozenset({"localhost"}), 1)
TWO = Topology("main", frozenset({"ch-0", "ch-1"}), 1)
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
    monkeypatch.setattr(cell_scan.settings, "ch_cluster", "main")
    ch = FakeCH().on(
        "FROM system.clusters",
        [{"host_name": "ch-0", "shard_num": 1}, {"host_name": "ch-1", "shard_num": 2}],
    )
    topology = await cell_scan.read_topology(ch)
    assert topology == Topology("main", frozenset({"ch-0", "ch-1"}), 2) and topology.multi
    assert "2 shards" in str(topology.refusals()[0])
    monkeypatch.setattr(cell_scan.settings, "ch_cluster", "")
    assert await cell_scan.read_topology(FakeCH()) == Topology("", frozenset(), 0)


async def test_each_host_answers_for_exactly_one_listed_name() -> None:
    ch = FakeCH().on(
        "clusterAllReplicas", [{"h": "os-0", "names": ["ch-0"]}, {"h": "os-1", "names": ["ch-1"]}]
    )
    assert await cell_scan.host_names(ch, TWO) == NAMES
    # Every clusterAllReplicas read waits for every replica and reads its own host locally.
    assert "skip_unavailable_shards = 0, prefer_localhost_replica = 1" in ch.sql("query_all")[0]


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
        await cell_scan.host_names(FakeCH().on("clusterAllReplicas", rows), TWO)


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
    monkeypatch.setattr(cell_scan.settings, "ch_database", "prod_loom")
    monkeypatch.setattr(cell_scan.settings, "ch_telemetry_database", "prod_telemetry")
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
    _, refusals = cell_scan.judge_clickhouse(replicated, _CLASSIFIED, Topology("", frozenset(), 0))
    assert "LOOM_CH_CLUSTER (unset) lists no host" in refusals[0].reason
    scanned, refusals = cell_scan.judge_clickhouse(replicated, _CLASSIFIED, ONE)
    assert refusals == [] and scanned == replicated
    # So does a shared one: an unlisted cluster is not known to be one host.
    shared = [Table("loom", "memory", "SharedReplacingMergeTree")]
    unlisted = Topology("main", frozenset(), 0)
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
        {"host": h, "port": 9000, "status": status, "error": "" if not status else "lost"}
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
    tables = TableList(refusals=[cell_scan.Refusal("clickhouse loom.x", "engine Memory")])
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
        def __init__(self, rows: list[dict[str, str]]) -> None:
            self.rows = rows

        async def fetchval(self, sql: str, *_: Any) -> bool:
            return True  # the org index exists

        async def fetch(self, sql: str) -> list[dict[str, str]]:
            return self.rows

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
    [_PgOutage("connection reset"), BackendUnavailableError("down")],
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
    _pg_sessions(monkeypatch, _Canceled("canceling statement due to statement timeout"))
    with pytest.raises(Refused, match="postgres loom.tenancy: the scan ran out of --scan-budget"):
        await cell_scan.scan(FakeCH(), TableList(postgres=["loom.tenancy"]), ONE, Budget(60))


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
