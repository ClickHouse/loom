"""The org scan `loom cell attach` runs on the first cell: which tables of the cell's stores
hold an `org` column, and the distinct orgs they hold (docs/architecture/cells.md,
"Attaching a cell").

The scan reads, one statement per table and no long transaction:

- every object with an `org` column in the configured ClickHouse databases, judged by engine
  (the MergeTree family and KeeperMap are scanned, views are skipped, any other engine
  refuses), and every table of Postgres schema `loom` with one;
- each store's list against its erasure classification, both ways, so a table the listing
  missed cannot go unscanned;
- on a ClickHouse cluster of more than one host, every host's list through
  `clusterAllReplicas`, which must agree, and each replicated table synced on every replica
  before it is read.

Log tables (`LOG_TABLES`) are listed and judged but never scanned: a log row keeps the org a
caller typed, before any check, so placing a log-only org would let anyone pin an org name
to this cell. Every refusal is a `Refusal`, returned by the listing and raised in `Refused`
by the scan, so attach and doctor print the same names and reasons."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from clickhouse_connect.driver.exceptions import DatabaseError

from server.cli.cell_ch import (
    TAG,
    WHOLE,
    WHOLE_SQL,
    Refusal,
    Refused,
    Topology,
    databases,
    per_host,
    quote,
)
from server.cli.cell_writers import own_refusals
from server.core import ch_errors, ch_names, pg
from server.core.cell import ORG_FREE, named_org, org_name_refusal
from server.core.cell_triggers import ORG_TABLES
from server.core.ch import query_tag
from server.core.ch_errors import error_code, error_line
from server.core.errors import BackendUnavailableError
from server.erasure.store.census import classified_tables
from server.erasure.store.steps_pg import PG_CENSUS, PG_NO_MEMORY_CONTENT

# Tables whose orgs are never placed: rows written before any check (a refused or
# unauthenticated request's log row keeps the org it named), and tables a view fills from
# one of them. `tests/unit/test_cell_scan_tables.py` holds every other table with an `org`
# column, so each new one is judged here deliberately.
LOG_TABLES = frozenset(
    {
        "loom.agent_events",
        # A request refused at validation writes its row with the org it named.
        "loom.code_search_trace",
        # Refreshable views reading loom.agent_events.
        "loom.agent_cost_rollup_daily",
        "loom.subject_last_seen",
        "loom_telemetry.sli_minute",
    }
)

# A refreshable view's temporary table while a refresh exchanges its target.
_EXCHANGE = re.compile(r"^\.tmp\.inner(_id)?\.")
# A provider transition's copies (server/erasure/store/provider_copies.py).
_PROVIDER_COPY = "_provider_rebuild_"
_SKIPPED_ENGINES = frozenset({"View", "MaterializedView"})

_RETRY_PAUSE_S = 1.0  # not a setting: the pause before a transient failure's retry
# How long past the budget a Postgres try may take before this side gives it up, so the
# server's own statement timeout fires first when the server answers.
_PG_MARGIN_S = 5.0  # not a setting: a margin over --scan-budget
_TIMEOUT_CODES = frozenset({159})  # TIMEOUT_EXCEEDED
_UNKNOWN_TABLE_CODES = frozenset({60, 81})  # UNKNOWN_TABLE, UNKNOWN_DATABASE

_sleep = asyncio.sleep  # the seam the retry tests replace


class Budget:
    """`--scan-budget`: the seconds one full scan may take, shared by its statements."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds = float(seconds)
        self._clock = clock
        self._start = clock()

    def elapsed(self) -> float:
        return self._clock() - self._start

    def left(self) -> float:
        return self.seconds - self.elapsed()

    def require(self, subject: str) -> float:
        """The seconds left, or Refused naming `subject` once none are."""
        left = self.left()
        if left <= 0:
            raise Refused([_out_of_budget(subject, self.seconds)])
        return left


def _out_of_budget(subject: str, seconds: float) -> Refusal:
    return Refusal(subject, f"the scan ran out of --scan-budget ({seconds:g} s): raise it")


def pg_quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


# -- the table list ----------------------------------------------------------------------


@dataclass(frozen=True)
class Table:
    """A ClickHouse object with an `org` column: its configured database, name, engine."""

    database: str
    name: str
    engine: str

    @property
    def canonical(self) -> str:
        """`loom.<name>` or `loom_telemetry.<name>`, as erasure's classification has it."""
        return f"{ch_names.logical(self.database)}.{self.name}"

    @property
    def sql(self) -> str:
        return f"{quote(self.database)}.{quote(self.name)}"


@dataclass
class TableList:
    """What the scan reads: the ClickHouse tables and the Postgres tables (`loom.<name>`),
    log tables left out, and every refusal the listing found."""

    clickhouse: list[Table] = field(default_factory=list)
    postgres: list[str] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)


_COLUMNS_SQL = (
    "SELECT hostName() AS h, database, table FROM {source:columns} "
    "WHERE database IN {dbs:Array(String)} AND name = 'org'"
)
_TABLES_SQL = (
    "SELECT hostName() AS h, database, name, engine FROM {source:tables} "
    "WHERE database IN {dbs:Array(String)}"
)


async def _host_listings(
    ch: Any, topology: Topology, names: Mapping[str, str], dbs: list[str]
) -> dict[str, dict[tuple[str, str], str]]:
    """Per configured host name, every object with an `org` column and its engine."""
    params = {"dbs": dbs}
    columns = await per_host(ch, topology, names, _COLUMNS_SQL, params)
    tables = await per_host(ch, topology, names, _TABLES_SQL, params)
    out: dict[str, dict[tuple[str, str], str]] = {}
    for host, rows in tables.items():
        with_org = {(str(r["database"]), str(r["table"])) for r in columns.get(host, [])}
        engines = {(str(r["database"]), str(r["name"])): str(r["engine"]) for r in rows}
        out[host] = {key: engines.get(key, "") for key in with_org}
    return out


def _listing_problems(listings: dict[str, dict[tuple[str, str], str]]) -> list[Refusal]:
    """Every host must list the same tables with the same engines."""
    out = []
    for key in sorted({key for listing in listings.values() for key in listing}):
        name = f"{key[0]}.{key[1]}"
        seen = {host: listing.get(key) for host, listing in sorted(listings.items())}
        for host, engine in seen.items():
            if engine is None:
                out.append(Refusal(f"host {host}", f"does not list {name}, which another lists"))
        if len({e for e in seen.values() if e is not None}) > 1:
            out += [
                Refusal(f"host {host}", f"lists {name} as {engine}: the hosts' engines differ")
                for host, engine in seen.items()
                if engine is not None
            ]
    return out


def _without_exchanges(listing: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
    return {k: v for k, v in listing.items() if not _EXCHANGE.match(k[1])}


async def list_clickhouse(
    ch: Any, topology: Topology, names: Mapping[str, str], dbs: list[str]
) -> tuple[list[Table], list[Refusal]]:
    """Every object with an `org` column in `dbs`, a refreshable view's exchange table left
    out; listed once more when one was seen or the hosts disagreed (a refresh runs on each
    host on its own schedule), and refused when the hosts still disagree."""
    listings = await _host_listings(ch, topology, names, dbs)
    seen_exchange = any(_without_exchanges(v) != v for v in listings.values())
    if seen_exchange or _listing_problems(listings):
        listings = await _host_listings(ch, topology, names, dbs)
    listings = {host: _without_exchanges(v) for host, v in listings.items()}
    if problems := _listing_problems(listings):
        return [], problems
    merged = next(iter(listings.values()), {})
    return [Table(db, name, engine) for (db, name), engine in sorted(merged.items())], []


def provider_copies(tables: Iterable[Table]) -> list[Refusal]:
    """A provider transition's copies refuse up front: erasure covers them by name, and the
    transition removes them before it ends."""
    return [
        Refusal(f"clickhouse {t.canonical}", "a provider transition is running")
        for t in tables
        if t.name.startswith(_PROVIDER_COPY)
    ]


def judge_clickhouse(
    tables: list[Table], classified: Iterable[str], topology: Topology
) -> tuple[list[Table], list[Refusal]]:
    """The tables to scan (log tables and views left out) and the refusals: an engine the
    scan cannot read, a table erasure does not classify, a table whose replicas cannot be
    synced or that a scan through one host would read only part of."""
    known = set(classified)
    scanned, refusals = [], []
    for t in tables:
        if t.engine in _SKIPPED_ENGINES:
            continue  # stores no rows: a TO view's target is listed itself
        if problem := _table_problem(t, known, topology):
            refusals.append(Refusal(f"clickhouse {t.canonical}", problem))
        elif t.canonical not in LOG_TABLES:
            scanned.append(t)
    return scanned, refusals


def _table_problem(t: Table, known: set[str], topology: Topology) -> str:
    if not (t.engine.endswith("MergeTree") or t.engine == "KeeperMap"):
        return f"engine {t.engine or 'unknown'} holds an org column the scan cannot read"
    if t.canonical not in known:
        return "missing from erasure's table classification"
    if t.engine.startswith(("Replicated", "Shared")) and not topology.hosts:
        return (
            f"replicated, and LOOM_CH_CLUSTER ({topology.cluster or 'unset'}) lists no host: "
            "its replicas cannot be synced"
        )
    shared = t.engine.startswith(("Replicated", "Shared")) or t.engine == "KeeperMap"
    if topology.multi and not shared:
        return (
            f"a {t.engine} table on a cluster of several hosts: a scan through one host "
            "would miss the others' rows"
        )
    return ""


async def reverse_clickhouse(
    ch: Any, listed: Iterable[Table], classified: Iterable[str]
) -> list[Refusal]:
    """A classified table the listing did not return, whose `DESCRIBE TABLE` (which errors
    rather than hides on a missing grant) shows an `org` column, refuses; so does a
    `DESCRIBE` refused for want of a grant. A table that does not exist is not scanned."""
    seen = {t.canonical for t in listed}
    out = []
    for name in sorted(set(classified) - seen):
        db, table = ch_names.split_table(name)
        try:
            rows = await ch.query_all(f"DESCRIBE TABLE {quote(db)}.{quote(table)} {WHOLE_SQL}")
        except Exception as e:  # noqa: BLE001 — every failure but a missing table refuses
            if error_code(e) in _UNKNOWN_TABLE_CODES:
                continue
            out.append(Refusal(f"clickhouse {name}", f"DESCRIBE TABLE failed: {error_line(e)}"))
            continue
        if any(str(r.get("name")) == "org" for r in rows):
            out.append(
                Refusal(f"clickhouse {name}", "has an org column the table listing did not return")
            )
    return out


def pg_classified() -> set[str]:
    """Postgres's erasure classification, as `loom.<name>`."""
    return {t.removeprefix("pg:") for t in PG_CENSUS} | set(PG_NO_MEMORY_CONTENT)


async def list_postgres(conn: Any) -> tuple[list[str], list[Refusal]]:
    """Every table of schema `loom` with an `org` column (`cell_triggers.ORG_TABLES`), and
    the refusals: one erasure does not classify, and a census table the listing leaves out
    (a partition, or a relation of another kind) whose `pg_attribute` shows an `org`
    column."""
    names = [f"loom.{r['relname']}" for r in await conn.fetch(ORG_TABLES)]
    known = pg_classified()
    refusals = [
        Refusal(f"postgres {n}", "missing from erasure's table classification")
        for n in names
        if n not in known
    ]
    for name in sorted({t.removeprefix("pg:") for t in PG_CENSUS} - set(names)):
        has_org = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass($1) "
            "AND attname = 'org' AND attnum > 0 AND NOT attisdropped)",
            name,
        )
        if has_org:
            refusals.append(
                Refusal(f"postgres {name}", "has an org column the table listing did not return")
            )
    return names, refusals


async def list_tables(
    ch: Any, pg_conn: Any, topology: Topology, names: Mapping[str, str]
) -> TableList:
    """The scan's table list in both stores, with every refusal it found, attach's own
    ClickHouse grants checked first. `names` is `cell_writers.host_names()` on a cluster of
    several hosts, else empty."""
    with query_tag(TAG):
        return await _list_tables(ch, pg_conn, topology, names)


async def _list_tables(
    ch: Any, pg_conn: Any, topology: Topology, names: Mapping[str, str]
) -> TableList:
    if refusals := topology.refusals():
        return TableList(refusals=refusals)
    # The system tables hide what the reading user cannot see: a narrower user lists short.
    lacking, _ = await own_refusals(ch, topology, names)
    if lacking:
        return TableList(refusals=lacking)
    classified = set(classified_tables())
    out = TableList()
    tables, problems = await list_clickhouse(ch, topology, names, databases())
    if problems:
        out.refusals += problems
        return out
    if copies := provider_copies(tables):
        out.refusals += copies
        return out
    out.clickhouse, problems = judge_clickhouse(tables, classified, topology)
    out.refusals += problems
    out.refusals += await reverse_clickhouse(ch, tables, classified)
    out.postgres, problems = await list_postgres(pg_conn)
    out.refusals += problems
    return out


# -- the orgs ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """The orgs found, each with the sources it was found in (`postgres loom.tenancy`,
    `clickhouse loom.memory`, `configured`); those no request can name (not UTF-8, a NUL),
    per source, as found (hex when not text); those longer than `cell.ORG_MAX_BYTES`, for
    attach to refuse; and the seconds the scan took."""

    orgs: dict[str, set[str]] = field(default_factory=dict)
    malformed: dict[str, list[str]] = field(default_factory=dict)
    too_long: dict[str, set[str]] = field(default_factory=dict)
    elapsed_s: float = 0.0

    def add(self, source: str, value: bytes | str | None) -> None:
        if value is None:
            self.malformed.setdefault(source, []).append("NULL")
            return
        if isinstance(value, bytes):
            try:
                text = value.decode()
            except UnicodeDecodeError:
                self.malformed.setdefault(source, []).append(value.hex())
                return
        else:
            text = value
        if text in ORG_FREE:
            return  # the sentinels: never an org of a cell
        if named_org(text) is None:
            self.malformed.setdefault(source, []).append(repr(text))
            return
        if org_name_refusal(text):
            self.too_long.setdefault(text, set()).add(source)
            return
        self.orgs.setdefault(text, set()).add(source)


def configured_orgs(*values: Mapping[str, Any] | None) -> set[str]:
    """The configured orgs of fingerprint values in their stored form (`loom.cell_self`'s
    `current_fp` and `next_fp`)."""
    out: set[str] = set()
    for value in values:
        fields = (value or {}).get("fields")
        if isinstance(fields, Mapping):
            out.update(str(o) for o in fields.get("configured_orgs") or [])
    return out


# The skip scan: one index probe per distinct org, each the next org past the last.
_PG_SKIP_SCAN = """
WITH RECURSIVE o(org) AS (
    (SELECT org FROM {table} WHERE org IS NOT NULL ORDER BY org LIMIT 1)
    UNION ALL
    SELECT n.org FROM o, LATERAL (
        SELECT t.org FROM {table} t WHERE t.org > o.org ORDER BY t.org LIMIT 1
    ) n
)
SELECT org FROM o
"""

# A plain btree index (not partial, not on an expression) whose first key is `org`, with the
# column's collation and the default operator class, so `ORDER BY org` and `>` use it.
_PG_ORG_INDEX = """
SELECT EXISTS (
    SELECT 1 FROM pg_index i
    JOIN pg_class ic ON ic.oid = i.indexrelid
    JOIN pg_am am ON am.oid = ic.relam
    JOIN pg_opclass opc ON opc.oid = i.indclass[0]
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'org'
    WHERE i.indrelid = to_regclass($1) AND am.amname = 'btree' AND i.indisvalid
      AND i.indkey[0] = a.attnum AND i.indpred IS NULL AND i.indexprs IS NULL
      AND i.indcollation[0] = a.attcollation AND opc.opcdefault
)
"""


def pg_skip_scan_sql(table: str) -> str:
    schema, _, name = table.partition(".")
    return _PG_SKIP_SCAN.format(table=f"{pg_quote(schema)}.{pg_quote(name)}")


async def _pg_orgs(table: str, budget: Budget) -> list[str]:
    subject = f"postgres {table}"
    while True:
        left = budget.require(subject)
        # The server's statement timeout cannot fire on a primary that vanished, and the
        # connection has no client timeout: the budget bounds the try on this side too.
        bound = asyncio.timeout(left + _PG_MARGIN_S)
        try:
            async with bound, pg.dedicated(statement_timeout_ms=max(1, int(left * 1000))) as conn:
                if not await conn.fetchval(_PG_ORG_INDEX, table):
                    raise Refused(
                        [
                            Refusal(
                                subject,
                                "has no plain btree index led by org: a scan would "
                                "read the whole table on the primary",
                            )
                        ]
                    )
                return [str(r["org"]) for r in await conn.fetch(pg_skip_scan_sql(table))]
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001 — a failed statement is retried or named
            failure = e
        # 57014 is also a cancel from outside (pg_cancel_backend), retried as an outage: the
        # budget ran out only when there is none left, since the timeout is what was left.
        timed_out = getattr(failure, "sqlstate", None) == "57014" and budget.left() <= 1
        if bound.expired() or timed_out:
            raise Refused([_out_of_budget(subject, budget.seconds)]) from None
        if not (pg.is_outage(failure) or isinstance(failure, BackendUnavailableError)):
            raise Refused([Refusal(subject, f"the scan failed: {failure}")]) from failure
        await _sleep(_RETRY_PAUSE_S)


def _ch_transient(e: BaseException) -> bool:
    return (
        ch_errors.is_transient_network_error(e)
        or ch_errors.is_connection_failure(e)
        or ch_errors.is_passing_state(e)
        or ch_errors.is_replica_bound_error(e)
    )


def _on_cluster(topology: Topology) -> str:
    return f" ON CLUSTER {quote(topology.cluster)}" if topology.multi else ""


async def _system(ch: Any, sql: str, topology: Topology, budget: Budget, subject: str) -> None:
    """A SYSTEM statement bounded by what is left of `budget`. On several hosts it carries
    `ON CLUSTER` (`_on_cluster`) and each host's status is read back: every listed host must
    answer, each with status 0 (pinned with `distributed_ddl_output_mode = 'throw'`, so a
    profile's `never_throw` hides none). One that does not finish in time refuses as the
    budget when the budget is spent, else with the server's error and the hosts it did not
    finish on: a replica that cannot sync."""
    seconds = max(1, int(budget.require(subject)))
    # `receive_timeout` bounds SYNC REPLICA's own wait.
    bounds = {"max_execution_time": seconds, "receive_timeout": seconds}
    what = sql.split(" ON CLUSTER ")[0].split(" `")[0]
    try:
        if not topology.multi:
            await ch.execute(sql, settings=bounds)
            return
        body = await ch.raw_bytes(
            sql,
            settings={
                **bounds,
                "distributed_ddl_output_mode": "throw",
                "distributed_ddl_task_timeout": seconds,
                "default_format": "JSONEachRow",
            },
        )
        rows = _body_rows(body.decode("utf-8", "replace").splitlines(), "status")
    except Exception as e:  # only a timeout is judged here; the rest pass to the caller
        if error_code(e) not in _TIMEOUT_CODES:
            raise
        # The statement was given the budget's whole seconds, so up to one is left over.
        if budget.left() <= 1:
            raise Refused([_out_of_budget(subject, budget.seconds)]) from None
        done = {str(r.get("host")) for r in getattr(e, "rows", ()) if str(r.get("status")) == "0"}
        unfinished = ", ".join(sorted(topology.hosts - done)) or "this server"
        raise Refused(
            [Refusal(subject, f"{what} did not finish on {unfinished}: {error_line(e)}")]
        ) from e
    failed = [r for r in rows if int(r["status"]) != 0]
    if failed:
        raise Refused(
            [Refusal(f"replica {r.get('host')}", f"{subject}: {r.get('error')}") for r in failed]
        )
    answered = {str(r.get("host")) for r in rows}
    if answered != topology.hosts:
        missing = ", ".join(sorted(topology.hosts - answered)) or "none"
        raise Refused(
            [
                Refusal(
                    subject, f"the hosts that answered are not the listed ones (missing: {missing})"
                )
            ]
        )


def ch_scan_sql(table: Table, topology: Topology) -> str:
    """The distinct orgs of `table` as hex (bytes: an org that is not UTF-8 is judged, never
    decoded into another name); on several hosts, first a row naming the configured host
    that ran it, which an empty table returns too."""
    orgs = f"SELECT 'o' AS k, hex(org) AS v FROM (SELECT DISTINCT org FROM {table.sql})"
    if not topology.multi:
        return f"{orgs} FORMAT JSONEachRow"
    host = (
        "SELECT 'h' AS k, toJSONString((SELECT groupArray(host_name) FROM system.clusters "
        "WHERE cluster = {c:String} AND is_local)) AS v"
    )
    return f"{host} UNION ALL {orgs} FORMAT JSONEachRow"


class BodyError(DatabaseError):
    """An error ClickHouse wrote into a response body after it began streaming rows: a
    server error as the driver raises one, its text keeping the server's `Code: <n>.`, so it
    is judged transient or not, and by code, as any other. `rows` are the rows before it."""

    def __init__(self, text: str, rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(text)
        self.rows = list(rows or [])


def _body_rows(lines: Iterable[str], key: str) -> list[dict[str, Any]]:
    """The JSONEachRow rows of a body, each holding `key`; BodyError at the first line that
    is not one (an `{"exception": …}` row, or the text tail a server appends once a
    statement fails after its first block), with the rest of the body as its text."""
    lines = list(lines)
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        if not isinstance(row, dict) or key not in row:
            exception = row.get("exception") if isinstance(row, dict) else None
            raise BodyError(str(exception or "\n".join(lines[i:]))[:2000], rows)
        rows.append(row)
    return rows


async def _read_scan(
    ch: Any, sql: str, params: dict[str, Any], extra: dict[str, Any]
) -> list[dict[str, Any]]:
    chunks: list[bytes] = []
    async with ch.raw_stream(sql, params, settings=extra) as stream:
        async for chunk in stream:
            chunks.append(chunk)
    return _body_rows(b"".join(chunks).decode("utf-8", "replace").splitlines(), "k")


async def _ch_orgs(ch: Any, table: Table, topology: Topology, budget: Budget) -> list[bytes | None]:
    subject = f"clickhouse {table.canonical}"
    while True:
        budget.require(subject)
        try:
            # Rows buffered on any replica land before the table is read.
            flush = f"SYSTEM FLUSH ASYNC INSERT QUEUE{_on_cluster(topology)}"
            await _system(ch, flush, topology, budget, subject)
            if table.engine.startswith("Replicated"):
                # Every replica pulls every part acknowledged before now, so the read
                # through any one of them sees them all.
                await _system(
                    ch,
                    f"SYSTEM SYNC REPLICA{_on_cluster(topology)} {table.sql} LIGHTWEIGHT",
                    topology,
                    budget,
                    subject,
                )
            extra: dict[str, Any] = {
                **WHOLE,
                "max_execution_time": max(1, int(budget.require(subject))),
            }
            if table.engine.startswith("Shared"):
                extra["select_sequential_consistency"] = 1
            params = {"c": topology.cluster} if topology.multi else {}
            rows = await _read_scan(ch, ch_scan_sql(table, topology), params, extra)
        except Refused:
            raise
        except Exception as e:  # noqa: BLE001 — a failed statement is retried or named
            failure = e
        else:
            if topology.multi:
                _check_scan_host(rows, topology, subject)
            return [
                None if r["v"] is None else bytes.fromhex(str(r["v"]))
                for r in rows
                if r["k"] == "o"
            ]
        if error_code(failure) in _TIMEOUT_CODES:
            raise Refused([_out_of_budget(subject, budget.seconds)]) from None
        if not _ch_transient(failure):
            raise Refused(
                [Refusal(subject, f"the scan failed: {error_line(failure)}")]
            ) from failure
        await _sleep(_RETRY_PAUSE_S)


def _check_scan_host(rows: list[dict[str, Any]], topology: Topology, subject: str) -> None:
    named = [json.loads(str(r["v"])) for r in rows if r["k"] == "h"]
    if len(named) != 1 or len(named[0]) != 1 or named[0][0] not in topology.hosts:
        raise Refused(
            [
                Refusal(
                    subject,
                    f"the scan's server named itself {named or 'nothing'}, not one "
                    f"host of cluster {topology.cluster}",
                )
            ]
        )


async def scan(
    ch: Any,
    tables: TableList,
    topology: Topology,
    budget: Budget,
    configured: Iterable[str] = (),
) -> ScanResult:
    """The distinct orgs of every table of `tables` and the `configured` orgs; Refused, with
    the listing's refusals before any read, and naming the table when a statement runs out
    of `budget` or fails other than transiently. A `NULL` org is named malformed. The caller
    connects ClickHouse inside `ch.statement_timeout()` of the budget, so the driver's own
    read timeout does not end a long statement first."""
    if tables.refusals:
        raise Refused(tables.refusals)
    result = ScanResult()
    for org in sorted(configured):
        result.add("configured", org)
    with query_tag(TAG):
        for name in tables.postgres:
            for org in await _pg_orgs(name, budget):
                result.add(f"postgres {name}", org)
        for table in tables.clickhouse:
            for raw in await _ch_orgs(ch, table, topology, budget):
                result.add(f"clickhouse {table.canonical}", raw)
    result.elapsed_s = budget.elapsed()
    return result
