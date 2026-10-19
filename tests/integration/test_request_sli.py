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


def test_each_row_is_folded_once_however_late_and_a_refresh_is_bounded(ch_client) -> None:
    """Rows stamped as ClickHouse would have taken them: a refresh folds at
    most an hour past the first row not folded yet, a row that arrives
    after newer ones were folded still counts in the minute of its `ts`, and
    nothing is folded twice."""
    from datetime import UTC, datetime, timedelta

    db = f"sli_probe_{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)

    def insert(ts: datetime, ingested_at: datetime, status: str) -> None:
        ch_client.insert(
            f"{db}.agent_events",
            [[ts, ingested_at, "o", "n", "a", "memory.get", uuid.uuid4().hex, "http", "read",
              status, status == "ok", "" if status == "ok" else "INTERNAL_ERROR", 20, 100,
              "request"]],
            column_names=["ts", "ingested_at", "org", "namespace", "agent", "operation",
                          "request_id", "transport", "slo_class", "status_class", "success",
                          "error_code", "latency_ms", "budget_ms", "kind"],
        )  # fmt: skip

    def refresh() -> tuple:
        ch_client.command(f"SYSTEM REFRESH VIEW {db}.sli_minute_refresh")
        ch_client.command(f"SYSTEM WAIT VIEW {db}.sli_minute_refresh")
        return ch_client.query(
            f"SELECT sum(requests), sum(server_errors), uniqExactIf(minute, slo_class != '') "
            f"FROM {db}.sli_minute"
        ).result_rows[0]

    try:
        _private_rollup(ch_client, db)
        insert(now - timedelta(hours=20), now - timedelta(hours=20), "server")
        insert(now - timedelta(hours=10), now - timedelta(hours=10), "ok")
        assert refresh() == (1, 1, 1)  # an hour past the first: the second waits
        assert refresh() == (2, 1, 2)
        # Nothing left to fold: the mark moves straight to the 70 s margin.
        assert refresh() == (2, 1, 2)
        lag = ch_client.query(
            f"SELECT dateDiff('second', max(ingested_hwm), now64(3)) FROM {db}.sli_minute "
            "WHERE slo_class = ''"
        ).result_rows[0][0]
        assert 70 <= lag < 80

        # Arrives now, past that mark, but its request ended 20 hours ago, in
        # the first row's minute.
        # Stamped by ClickHouse's clock, which the mark is read against.
        time.sleep(2)
        server_now = ch_client.query("SELECT now64(3)").result_rows[0][0].replace(tzinfo=UTC)
        insert(now - timedelta(hours=20), server_now - timedelta(seconds=70.5), "server")
        assert refresh() == (3, 2, 2)
        assert refresh() == (3, 2, 2)
    finally:
        ch_client.command(f"DROP DATABASE IF EXISTS {db} SYNC")


def test_the_view_refresh_gauges_name_the_rollup_with_its_database(ch_client) -> None:
    """`loom.ch.view_refresh.*` covers the rollup's refresh, named outside `loom`."""
    from server.observability.otel_probes import _VIEW_REFRESH_SQL

    views = {row[0] for row in ch_client.query(_VIEW_REFRESH_SQL).result_rows}
    assert "loom_telemetry.sli_minute_refresh" in views
