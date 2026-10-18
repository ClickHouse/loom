"""The request SLI path: a ledger row carries its transport, SLO class, status
class and budget, and `loom_telemetry.sli_minute` rolls the rows up per minute."""

from __future__ import annotations

import sys
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


@pytest.mark.asyncio
async def test_the_rollup_counts_slow_and_rate_limited_rows_and_skips_unclassified_ones(
    ch_client,
) -> None:
    """Rows written as the ledger writes them, stamped now: a refresh folds
    rows at least 10 s old past the newest one folded, so a row stamped
    earlier could fall behind what the scheduled refreshes already folded.
    One has no transport, as a build without the SLI columns writes it, and
    one no SLO class, as MCP's initialize; both are left out."""
    from datetime import UTC, datetime

    org = unique_org("sli-rollup")
    ts = datetime.now(UTC)
    base = {"org": org, "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    rows = [
        # (transport, slo_class, success, error_code, status_class, latency_ms, budget_ms)
        ("http", "read", True, "", "ok", 50, 100),
        ("http", "read", True, "", "ok", 500, 100),  # slow
        ("mcp", "read", False, "RATE_LIMITED", "client", 0, 100),
        ("http", "read", False, "INTERNAL_ERROR", "server", 20, 100),
        ("", "", False, "INTERNAL_ERROR", "", 20, 0),  # an older build's row
        ("mcp", "", True, "", "ok", 5, 0),  # no registered op
    ]
    ch_client.insert(
        "loom.agent_events",
        [
            [
                ts,
                *base.values(),
                "memory.get",
                uuid.uuid4().hex,
                t,
                c,
                s,
                b,
                ok,
                e,
                lat,
                "request",
            ]
            for t, c, ok, e, s, lat, b in rows
        ],
        column_names=[
            "ts",
            *base.keys(),
            "operation",
            "request_id",
            "transport",
            "slo_class",
            "status_class",
            "budget_ms",
            "success",
            "error_code",
            "latency_ms",
            "kind",
        ],  # fmt: skip
    )

    def _rolled() -> tuple | None:
        got = ch_client.query(
            "SELECT sum(requests), sum(server_errors), sum(client_errors), sum(rate_limited), "
            "sum(slow), quantilesTDigestMerge(0.5, 0.95, 0.99)(latency)[3] "
            "FROM loom_telemetry.sli_minute WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows[0]
        return got if got[0] else None

    async def _seen() -> tuple | None:
        # Nothing qualifies for the first 10 s; refreshing each poll folds them then.
        ch_client.command("SYSTEM REFRESH VIEW loom_telemetry.sli_minute_refresh")
        return _rolled()

    requests, server, client, limited, slow, p99 = await eventually(
        _seen, max_wait=45.0, interval=2.0, description="the rollup of the inserted rows"
    )
    assert (requests, server, client, limited, slow) == (4, 1, 1, 1, 1)
    assert 50 <= p99 <= 500  # successful requests only
