"""The request SLI path: a ledger row carries its transport, SLO class, status
class and budget, and `loom_telemetry.sli_minute` rolls the rows up per minute."""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("sli")


def _ledger(ch_client, ns: str) -> list[tuple]:
    return ch_client.query(
        "SELECT operation, transport, slo_class, status_class, budget_ms, success "
        "FROM loom.agent_events WHERE org = {org:String} AND namespace = {ns:String} "
        "ORDER BY operation, success",
        parameters={"org": ORG, "ns": ns},
    ).result_rows


@pytest.mark.asyncio
async def test_ledger_rows_carry_the_sli_columns(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = f"sli-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="a", base_url=control_plane_url) as a:
        await a.memory.set("k", "the sli path")
        await a.memory.search("sli path")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as c:
        r = await c.post(
            "/v1/memory.get",
            json={"org": ORG, "namespace": ns, "agent": "a"},  # no memory_key
        )
        assert r.status_code == 422, r.text

    async def _three() -> list[tuple] | None:
        rows = _ledger(ch_client, ns)
        return rows if len(rows) >= 3 else None

    rows = await eventually(_three, max_wait=10.0, description="three ledger rows")
    by_op = {r[0]: r for r in rows}
    assert by_op["memory.set"][1:4] == ("http", "write", "ok")
    assert by_op["memory.search"][1:4] == ("http", "search", "ok")
    assert by_op["memory.get"][1:4] == ("http", "read", "client")
    assert all(r[4] > 0 for r in rows), rows


@pytest.mark.asyncio
async def test_an_mcp_call_is_ledgered_with_its_transport(
    control_plane_url: str, ch_client, mint_test_token
) -> None:
    ns = f"sli-mcp-{uuid.uuid4().hex[:6]}"
    token = mint_test_token(org=ORG, namespaces=[ns], subject="sli-test")
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "loom_memory_set",
            "arguments": {
                "org": ORG,
                "namespace": ns,
                "agent": "a",
                "memory_key": "k",
                "value": "v",
            },
        },
    }
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as c:
        r = await c.post("/mcp", json=call, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["result"]["isError"] is False, r.text

    async def _row() -> list[tuple] | None:
        return _ledger(ch_client, ns) or None

    rows = await eventually(_row, max_wait=10.0, description="the MCP ledger row")
    assert rows[0][:4] == ("memory.set", "mcp", "write", "ok")
    assert rows[0][4] > 0


def _insert_requests(ch_client, org: str, ts, rows: list[tuple]) -> None:
    """Ledger request rows as the ledger writes them, one insert, all at `ts`;
    ClickHouse stamps `ingested_at`. Each row is (operation, transport,
    slo_class, success, error_code, status_class, latency_ms, budget_ms)."""
    base = {"org": org, "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    columns = ["ts", *base, "operation", "request_id", "transport", "slo_class", "success",
               "error_code", "status_class", "latency_ms", "budget_ms", "kind"]  # fmt: skip
    ch_client.insert(
        "loom.agent_events",
        [[ts, *base.values(), r[0], uuid.uuid4().hex, *r[1:], "request"] for r in rows],
        column_names=columns,
    )


def _rolled(ch_client, org: str) -> tuple:
    """(requests, server errors, client errors, rate limited, slow, p99) in
    `sli_minute` for `org`."""
    return ch_client.query(
        "SELECT sum(requests), sum(server_errors), sum(client_errors), sum(rate_limited), "
        "sum(slow), quantilesTDigestMerge(0.5, 0.95, 0.99)(latency)[3] "
        "FROM loom_telemetry.sli_minute WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows[0]


async def _rolled_once(ch_client, org: str, requests: int) -> tuple:
    """Refresh until `org` has `requests` folded. Rows qualify 70 s after
    ClickHouse took them, so the first polls fold nothing."""

    async def _seen() -> tuple | None:
        ch_client.command("SYSTEM REFRESH VIEW loom_telemetry.sli_minute_refresh")
        ch_client.command("SYSTEM WAIT VIEW loom_telemetry.sli_minute_refresh")
        got = _rolled(ch_client, org)
        return got if got[0] >= requests else None

    return await eventually(_seen, max_wait=150.0, interval=5.0, description="the rollup")


@pytest.mark.asyncio
async def test_the_rollup_counts_slow_and_rate_limited_rows_and_skips_unclassified_ones(
    ch_client,
) -> None:
    """The two with no SLO class are left out: one with no transport either, as
    the `/v1/me/*` routes write theirs, and one for a call that names no
    registered op."""
    from datetime import UTC, datetime

    org = unique_org("sli-rollup")
    _insert_requests(
        ch_client,
        org,
        datetime.now(UTC),
        [
            ("memory.get", "http", "read", True, "", "ok", 50, 100),
            ("memory.get", "http", "read", True, "", "ok", 500, 100),  # slow
            ("memory.get", "mcp", "read", False, "RATE_LIMITED", "client", 0, 100),
            # a server error, not in the quantiles
            ("memory.get", "http", "read", False, "INTERNAL_ERROR", "server", 5000, 100),
            # no transport or SLO class, as a /v1/me/* route writes it
            ("me.sharing-summary", "", "", False, "HTTP_500", "server", 20, 0),
            ("mcp.initialize", "mcp", "", True, "", "ok", 5, 0),  # no registered op
        ],
    )
    requests, server, client, limited, slow, p99 = await _rolled_once(ch_client, org, 4)
    assert (requests, server, client, limited, slow) == (4, 1, 1, 1, 1)
    assert 50 <= p99 <= 500  # the failure's 5000 ms is not among the latencies

    # The next refresh resumes past the high-water mark: nothing is folded twice.
    ch_client.command("SYSTEM REFRESH VIEW loom_telemetry.sli_minute_refresh")
    ch_client.command("SYSTEM WAIT VIEW loom_telemetry.sli_minute_refresh")
    assert _rolled(ch_client, org)[:5] == (4, 1, 1, 1, 1)


def _private_rollup(ch_client, db: str) -> None:
    """The migration's rollup view over private copies of its two tables, so a
    test can stamp `ingested_at` itself without moving the shared mark. Its
    refreshes run only when the test asks."""
    view = (ROOT / "schemas/migrations/043_request_sli.sql").read_text()
    view = view[view.index("CREATE MATERIALIZED VIEW") :].rstrip().rstrip(";")
    view = view.replace("REFRESH EVERY 1 MINUTE", "REFRESH EVERY 1 YEAR")
    view = view.replace("loom.agent_events", f"{db}.agent_events")
    view = view.replace("loom_telemetry.sli_minute", f"{db}.sli_minute")
    view = view.replace("DEFINER = CURRENT_USER", "EMPTY DEFINER = CURRENT_USER")
    ch_client.command(f"CREATE DATABASE {db}")
    ch_client.command(f"CREATE TABLE {db}.agent_events AS loom.agent_events")
    ch_client.command(f"CREATE TABLE {db}.sli_minute AS loom_telemetry.sli_minute")
    ch_client.command(view)


def _insert_private(
    ch_client, db: str, ts, ingested_at, status: str, *, latency_ms: int = 20, budget_ms: int = 100
) -> None:
    """One ledger request row in `db`, stamped `ingested_at` as ClickHouse would."""
    ch_client.insert(
        f"{db}.agent_events",
        [[ts, ingested_at, "o", "n", "a", "memory.get", uuid.uuid4().hex, "http", "read",
          status, status == "ok", "" if status == "ok" else "INTERNAL_ERROR", latency_ms,
          budget_ms, "request"]],
        column_names=["ts", "ingested_at", "org", "namespace", "agent", "operation",
                      "request_id", "transport", "slo_class", "status_class", "success",
                      "error_code", "latency_ms", "budget_ms", "kind"],
    )  # fmt: skip


def _refresh_private(ch_client, db: str) -> tuple:
    """Refresh `db`'s rollup once: (requests, server errors, slow, data minutes)."""
    ch_client.command(f"SYSTEM REFRESH VIEW {db}.sli_minute_refresh")
    ch_client.command(f"SYSTEM WAIT VIEW {db}.sli_minute_refresh")
    return ch_client.query(
        f"SELECT sum(requests), sum(server_errors), sum(slow), "
        f"uniqExactIf(minute, slo_class != '') FROM {db}.sli_minute"
    ).result_rows[0]


def _server_now(ch_client):
    from datetime import UTC

    return ch_client.query("SELECT now64(3)").result_rows[0][0].replace(tzinfo=UTC)


def test_each_row_is_folded_once_however_late_and_a_refresh_is_bounded(ch_client) -> None:
    """Rows stamped as ClickHouse would have taken them: a refresh folds at
    most an hour past the first row not folded yet, a row that arrives
    after newer ones were folded still counts in the minute of its `ts`, and
    nothing is folded twice."""
    from datetime import UTC, datetime, timedelta

    db = f"sli_probe_{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    try:
        _private_rollup(ch_client, db)
        _insert_private(
            ch_client, db, now - timedelta(hours=20), now - timedelta(hours=20), "server"
        )
        _insert_private(ch_client, db, now - timedelta(hours=10), now - timedelta(hours=10), "ok")
        # an hour past the first: the second waits
        assert _refresh_private(ch_client, db) == (1, 1, 0, 1)
        assert _refresh_private(ch_client, db) == (2, 1, 0, 2)
        # Nothing left to fold: the mark moves straight to the 70 s margin.
        assert _refresh_private(ch_client, db) == (2, 1, 0, 2)
        lag = ch_client.query(
            f"SELECT dateDiff('second', max(ingested_hwm), now64(3)) FROM {db}.sli_minute "
            "WHERE slo_class = ''"
        ).result_rows[0][0]
        assert 70 <= lag < 80

        # Arrives now, past that mark, but its request ended 20 hours ago, in
        # the first row's minute.
        # Stamped by ClickHouse's clock, which the mark is read against.
        time.sleep(2)
        late = _server_now(ch_client) - timedelta(seconds=70.5)
        _insert_private(ch_client, db, now - timedelta(hours=20), late, "server")
        assert _refresh_private(ch_client, db) == (3, 2, 0, 2)
        assert _refresh_private(ch_client, db) == (3, 2, 0, 2)
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_a_refresh_commits_in_one_part_across_a_month_boundary(ch_client) -> None:
    """Rows on both sides of a month boundary and the sentinel land in one
    part, which commits whole: a failed refresh leaves neither data without
    its mark (folded twice on retry) nor a mark without its data (never
    folded)."""
    from datetime import UTC, datetime, timedelta

    db = f"sli_probe_{uuid.uuid4().hex[:8]}"
    month = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stamped = _server_now(ch_client) - timedelta(minutes=5)
    try:
        _private_rollup(ch_client, db)
        ch_client.command(f"SYSTEM STOP MERGES {db}.sli_minute")
        _insert_private(ch_client, db, month - timedelta(seconds=1), stamped, "server")
        _insert_private(ch_client, db, month, stamped, "ok")
        assert _refresh_private(ch_client, db) == (2, 1, 0, 2)
        parts = ch_client.query(
            "SELECT count(), sum(rows) FROM system.parts "
            "WHERE database = {db:String} AND table = 'sli_minute' AND active",
            parameters={"db": db},
        ).result_rows[0]
        assert parts == (1, 3)  # both months' rows and the sentinel
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_a_zero_budget_makes_every_success_slow(ch_client) -> None:
    """budget.set's 0 is a real budget, which every request exceeds."""
    from datetime import timedelta

    db = f"sli_probe_{uuid.uuid4().hex[:8]}"
    stamped = _server_now(ch_client) - timedelta(minutes=5)
    try:
        _private_rollup(ch_client, db)
        _insert_private(ch_client, db, stamped, stamped, "ok", latency_ms=25, budget_ms=0)
        assert _refresh_private(ch_client, db) == (1, 0, 1, 1)
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_rows_committed_after_the_mark_passed_them_are_counted_as_missed(ch_client) -> None:
    """A ledger part that becomes visible after the rollup's mark passed its
    rows is never folded; `loom.sli.rollup_missed_rows` counts them."""
    from datetime import timedelta

    from server.observability.otel_probes import _SLI_MISSED_SQL

    db = f"sli_probe_{uuid.uuid4().hex[:8]}"
    missed_sql = _SLI_MISSED_SQL.replace("loom.agent_events", f"{db}.agent_events")
    missed_sql = missed_sql.replace("loom_telemetry.sli_minute", f"{db}.sli_minute")
    stamped = _server_now(ch_client) - timedelta(minutes=5)
    try:
        _private_rollup(ch_client, db)
        _insert_private(ch_client, db, stamped, stamped, "ok")
        assert _refresh_private(ch_client, db) == (1, 0, 0, 1)
        assert ch_client.query(missed_sql).result_rows[0][0] == 0
        # Visible only now, stamped before the mark.
        _insert_private(ch_client, db, stamped, stamped + timedelta(seconds=1), "server")
        assert _refresh_private(ch_client, db) == (1, 0, 0, 1)
        assert ch_client.query(missed_sql).result_rows[0][0] == 1
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_the_view_refresh_gauges_name_the_rollup_with_its_database(ch_client) -> None:
    """`loom.ch.view_refresh.*` covers the rollup's refresh, named outside `loom`."""
    from server.observability.otel_probes import _VIEW_REFRESH_SQL

    views = {row[0] for row in ch_client.query(_VIEW_REFRESH_SQL).result_rows}
    assert "loom_telemetry.sli_minute_refresh" in views
