"""cost.summary — the agent_cost_rollup_daily roll-up plus the live tail.

Each Loom operation emits one row in `loom.agent_events`; a refreshable view
folds them once a minute into `loom.agent_cost_rollup_daily`, and cost.summary
adds the events newer than the roll-up's mark, finalizing with *Merge. Tests
prime events by exercising the SDK (events flush every ~100ms), wait for them
to land, then query.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _utc_today() -> date:
    """Today's date in UTC.

    agent_events.ts and agent_cost_rollup_daily.day are UTC-derived
    (toDate(ts) on a UTC DateTime). If the test runs from a host in
    a timezone ahead of UTC (e.g. IST = UTC+5:30), Python's
    date.today() returns tomorrow-UTC at the local-day rollover —
    the rollup query for "today" then excludes every row written
    in the last few hours, and total_ops comes back as 0.
    """
    return datetime.now(UTC).date()


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


async def _wait_for_events(
    ch_client: Any, namespace: str, expected_min: int, timeout_s: float = 5.0
) -> int:
    """Poll until agent_events has >= expected_min rows for this ns. The
    cost.summary handler reads the refreshed roll-up plus the agent_events
    rows newer than its mark, so a visible event is a counted one."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        rows = ch_client.query(
            "SELECT count() FROM loom.agent_events WHERE namespace = {ns:String}",
            parameters={"ns": namespace},
        ).result_rows
        last = int(rows[0][0])
        if last >= expected_min:
            return last
        await asyncio.sleep(0.1)
    return last


@pytest.mark.asyncio
async def test_empty_namespace_returns_zero_totals(control_plane_url: str) -> None:
    """A namespace with no events → empty rows, all totals zero."""
    from loom import Agent

    ns = f"cost-empty-{uuid.uuid4().hex[:6]}"
    async with Agent(org="cost-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        today = _utc_today()
        result = await a.cost.summary(
            from_date=today - timedelta(days=7),
            to_date=today,
        )
    assert result["rows"] == []
    assert result["total_ops"] == 0
    assert result["total_errors"] == 0
    assert result["total_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_summary_surfaces_emitted_events(control_plane_url: str, ch_client) -> None:
    """After firing N ops, cost.summary reports ops>=N for that namespace."""
    from loom import Agent

    ns = f"cost-fill-{uuid.uuid4().hex[:6]}"
    async with Agent(org="cost-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(5):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
        # Wait for the emitter to flush
        landed = await _wait_for_events(ch_client, ns, expected_min=5)
        assert landed >= 5

        today = _utc_today()
        result = await a.cost.summary(
            from_date=today,
            to_date=today,
            group_by=["operation"],
        )

    assert result["total_ops"] >= 5
    # One row per distinct operation (memory.set + cost.summary itself
    # would have emitted, but cost.summary fires AFTER the query, so the
    # rollup window mostly contains memory.set).
    ops_by_name = {r["operation"]: r for r in result["rows"]}
    assert "memory.set" in ops_by_name
    assert ops_by_name["memory.set"]["ops"] >= 5
    assert ops_by_name["memory.set"]["errors"] == 0
    # Latency quantiles must be set (positive)
    assert ops_by_name["memory.set"]["latency_p50_ms"] >= 0
    assert (
        ops_by_name["memory.set"]["latency_p99_ms"] >= ops_by_name["memory.set"]["latency_p50_ms"]
    )


@pytest.mark.asyncio
async def test_summary_filters_by_operation(control_plane_url: str, ch_client) -> None:
    """The optional `operation` filter pins the rollup to a single op."""
    from loom import Agent

    ns = f"cost-flt-{uuid.uuid4().hex[:6]}"
    async with Agent(org="cost-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
        await _wait_for_events(ch_client, ns, expected_min=3)

        today = _utc_today()
        result = await a.cost.summary(
            from_date=today,
            to_date=today,
            group_by=["operation"],
            operation="memory.set",
        )
    # Every row must be memory.set
    assert result["rows"]
    assert all(r["operation"] == "memory.set" for r in result["rows"])


@pytest.mark.asyncio
async def test_summary_groups_by_multiple_dimensions(control_plane_url: str, ch_client) -> None:
    """group_by=[operation, agent] gives one row per (op, agent) combo."""
    from loom import Agent

    ns = f"cost-mg-{uuid.uuid4().hex[:6]}"
    org = "cost-test"
    base = control_plane_url

    async with Agent(org=org, namespace=ns, agent="alpha", base_url=base) as a:
        for i in range(2):
            await a.memory.set(f"a{i}", {"v": i}, memory_type="semantic")
    async with Agent(org=org, namespace=ns, agent="beta", base_url=base) as a:
        for i in range(2):
            await a.memory.set(f"b{i}", {"v": i}, memory_type="semantic")
    await _wait_for_events(ch_client, ns, expected_min=4)

    async with Agent(org=org, namespace=ns, agent="reader", base_url=base) as r:
        today = _utc_today()
        result = await r.cost.summary(
            from_date=today,
            to_date=today,
            group_by=["operation", "agent"],
        )
    # Should see distinct (operation, agent) rows for alpha + beta on memory.set
    seen = {(r["operation"], r["agent"]) for r in result["rows"]}
    assert ("memory.set", "alpha") in seen
    assert ("memory.set", "beta") in seen


@pytest.mark.asyncio
async def test_org_wide_summary_aggregates_namespaces(
    control_plane_url: str,
    ch_client,
) -> None:
    """namespace=None aggregates across all the org's namespaces."""
    from loom import Agent

    org = f"cost-org-{uuid.uuid4().hex[:6]}"
    ns_a = f"a-{uuid.uuid4().hex[:6]}"
    ns_b = f"b-{uuid.uuid4().hex[:6]}"

    async with Agent(org=org, namespace=ns_a, agent="w", base_url=control_plane_url) as a:
        for i in range(2):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
    async with Agent(org=org, namespace=ns_b, agent="w", base_url=control_plane_url) as a:
        for i in range(2):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
    await _wait_for_events(ch_client, ns_a, expected_min=2)
    await _wait_for_events(ch_client, ns_b, expected_min=2)

    # Reader needs PLATFORM_ADMIN to see the org-wide rollup —
    # cost.summary is scope-aware, so a
    # plain writer only sees its own namespace. Mint an admin token
    # for the reader specifically.
    from server.auth.jwt import mint_token

    admin_tok = mint_token(
        subject="cost-test-admin",
        org=org,
        namespaces=["*"],
        roles=["platform_admin"],
        ttl_seconds=600,
        is_service_account=True,
    )
    async with Agent(
        org=org,
        namespace="reader-only",
        agent="r",
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as r:
        today = _utc_today()
        # group_by namespace so we see both namespaces in the result
        result = await r.cost.summary(
            from_date=today,
            to_date=today,
            namespace=None,
            group_by=["namespace", "operation"],
        )

    seen = {(row["namespace"], row["operation"]) for row in result["rows"]}
    assert (ns_a, "memory.set") in seen
    assert (ns_b, "memory.set") in seen
    assert result["total_ops"] >= 4  # at least the 4 memory.sets


@pytest.mark.asyncio
async def test_summary_date_window_excludes_past(control_plane_url: str, ch_client) -> None:
    """A from_date/to_date window in the past returns no current-day rows."""
    from loom import Agent

    ns = f"cost-pst-{uuid.uuid4().hex[:6]}"
    async with Agent(org="cost-test", namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
        await _wait_for_events(ch_client, ns, expected_min=3)

        # Window: 30 days ago — should return no current-day data
        old = date.today() - timedelta(days=30)
        result = await a.cost.summary(from_date=old, to_date=old)
    assert result["total_ops"] == 0
    assert result["rows"] == []
