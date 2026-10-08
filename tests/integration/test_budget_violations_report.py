"""POST /v1/admin/budget.violations — cross-(ns, op) aggregation endpoint.

Forces violations via the budget-override admin endpoint, then verifies
the report endpoint surfaces them grouped + aggregated.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("budget-rpt")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


async def _set_budget(c: httpx.AsyncClient, op: str, ms: int) -> None:
    r = await c.post("/v1/admin/budget.set", json={"op": op, "budget_ms": ms})
    assert r.status_code == 200, r.text


async def _clear_budget(c: httpx.AsyncClient, op: str) -> None:
    r = await c.post("/v1/admin/budget.set", json={"op": op, "clear": True})
    assert r.status_code == 200


async def _wait_for_violation(
    ch_client, ns: str, op: str, min_count: int = 1, timeout_s: float = 3.0
) -> int:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        rows = ch_client.query(
            """
            SELECT count() FROM loom.events
            WHERE org = {org:String} AND namespace = {ns:String}
              AND kind = 'budget_violation'
              AND position(payload, {op:String}) > 0
            """,
            parameters={"org": ORG, "ns": ns, "op": op},
        ).result_rows
        last = int(rows[0][0])
        if last >= min_count:
            return last
        await asyncio.sleep(0.1)
    return last


@pytest.mark.asyncio
async def test_no_violations_returns_empty(control_plane_url: str) -> None:
    """An org with no budget.violated rows returns an empty list."""
    isolated_org = f"no-violations-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post(
            "/v1/admin/budget.violations",
            json={"since_seconds": 60, "org": isolated_org},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["total_violations"] == 0
    assert body["since_seconds"] == 60


@pytest.mark.asyncio
async def test_violations_get_grouped_by_ns_and_op(control_plane_url: str, ch_client) -> None:
    """Multiple violations across the same (ns, op) get aggregated to one row."""
    from loom import Agent

    ns = f"rpt-{uuid.uuid4().hex[:6]}"

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _set_budget(c, "memory.set", 0)  # force violations
        try:
            # Fire 3 memory.set calls — all should violate the 0ms budget
            async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
                for i in range(3):
                    await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
            await _wait_for_violation(ch_client, ns, "memory.set", min_count=3)

            r = await c.post("/v1/admin/budget.violations", json={"since_seconds": 600, "org": ORG})
        finally:
            await _clear_budget(c, "memory.set")

    body = r.json()
    rows = [row for row in body["rows"] if row["namespace"] == ns]
    assert len(rows) == 1, f"expected one row for (ns, memory.set); got {rows}"
    row = rows[0]
    assert row["operation"] == "memory.set"
    assert row["violations"] == 3
    assert row["max_overage_ratio"] >= 0
    assert row["avg_overage_ratio"] >= 0
    assert row["last_violation_at"] is not None


@pytest.mark.asyncio
async def test_org_filter_scopes_results(control_plane_url: str, ch_client) -> None:
    """An org filter excludes rows from other orgs."""
    from loom import Agent

    ns_keep = f"rpt-keep-{uuid.uuid4().hex[:6]}"
    other_org = f"other-{uuid.uuid4().hex[:6]}"
    ns_drop = f"rpt-drop-{uuid.uuid4().hex[:6]}"

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        await _set_budget(c, "memory.set", 0)
        try:
            async with Agent(
                org=ORG, namespace=ns_keep, agent="w", base_url=control_plane_url
            ) as a:
                await a.memory.set("k", {"v": 1}, memory_type="semantic")
            async with Agent(
                org=other_org, namespace=ns_drop, agent="w", base_url=control_plane_url
            ) as a:
                await a.memory.set("k", {"v": 1}, memory_type="semantic")
            await _wait_for_violation(ch_client, ns_keep, "memory.set")
            await _wait_for_violation(ch_client, ns_drop, "memory.set")

            r = await c.post("/v1/admin/budget.violations", json={"since_seconds": 600, "org": ORG})
        finally:
            await _clear_budget(c, "memory.set")

    body = r.json()
    namespaces = {row["namespace"] for row in body["rows"]}
    assert ns_keep in namespaces, "ns_keep should appear under ORG filter"
    assert ns_drop not in namespaces, "ns_drop is in other_org; must not leak"


@pytest.mark.asyncio
async def test_since_seconds_window_excludes_old(control_plane_url: str, ch_client) -> None:
    """A tiny since_seconds window excludes older violations (use ts in the
    past to simulate)."""
    # Insert a synthetic budget.violated row from 1 hour ago
    ns = f"rpt-old-{uuid.uuid4().hex[:6]}"
    ch_client.command(
        """
        INSERT INTO loom.events
          (ts, kind, org, namespace, actor, payload, request_id)
        VALUES (now64(3) - INTERVAL 1 HOUR, 'budget_violation',
                {org:String}, {ns:String}, 'control-plane',
                '{"event_type": "budget.violated", "operation": "memory.set", "overage_ratio": 1.5}',
                'old-request-id')
        """,
        parameters={"org": ORG, "ns": ns},
    )

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        # since_seconds=60 → look back 1 minute. The synthetic row is 1 hour old.
        r = await c.post("/v1/admin/budget.violations", json={"since_seconds": 60, "org": ORG})

    body = r.json()
    assert not any(row["namespace"] == ns for row in body["rows"]), (
        "1h-old row must be outside a 60s window"
    )


@pytest.mark.asyncio
async def test_validation_rejects_bad_since_seconds(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post("/v1/admin/budget.violations", json={"since_seconds": 0})
    assert r.status_code == 422
    assert "since_seconds" in r.text
