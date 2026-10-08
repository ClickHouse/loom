"""POST /v1/admin/events.tail — recent agent_events firehose."""

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

ORG = unique_org("evt")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


async def _wait_events(ch_client, ns: str, expected: int, timeout_s: float = 3.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        rows = ch_client.query(
            "SELECT count() FROM loom.agent_events WHERE namespace = {ns:String}",
            parameters={"ns": ns},
        ).result_rows
        n = int(rows[0][0])
        if n >= expected:
            return n
        await asyncio.sleep(0.1)
    return n


@pytest.mark.asyncio
async def test_events_tail_returns_recent_rows(
    control_plane_url: str,
    ch_client,
) -> None:
    from loom import Agent

    ns = f"evt-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
    await _wait_events(ch_client, ns, expected=3)

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post("/v1/admin/events.tail", json={"namespace": ns, "since_seconds": 300})
    body = r.json()
    assert len(body["rows"]) >= 3
    assert all(row["namespace"] == ns for row in body["rows"])


@pytest.mark.asyncio
async def test_events_tail_filters_by_operation(
    control_plane_url: str,
    ch_client,
) -> None:
    from loom import Agent

    ns = f"evt-op-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
        await a.memory.delete("k")
        await a.memory.set("k2", {"v": 2}, memory_type="semantic")
    await _wait_events(ch_client, ns, expected=3)

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post(
            "/v1/admin/events.tail",
            json={"namespace": ns, "operation": "memory.delete", "since_seconds": 300},
        )
    rows = r.json()["rows"]
    assert len(rows) >= 1
    assert all(row["operation"] == "memory.delete" for row in rows)


@pytest.mark.asyncio
async def test_events_tail_filters_by_success_false(
    control_plane_url: str,
    ch_client,
) -> None:
    """Trigger a validation error (failed event) and confirm it shows up."""
    ns = f"evt-fail-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        # Missing required fields → validation failure → success=false event
        await c.post(
            "/v1/memory.set",
            json={
                "org": ORG,
                "namespace": ns,  # missing agent, memory_key, etc.
            },
        )
    await _wait_events(ch_client, ns, expected=1)

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post(
            "/v1/admin/events.tail",
            json={"namespace": ns, "success": False, "since_seconds": 300},
        )
    rows = r.json()["rows"]
    assert len(rows) >= 1
    assert all(row["success"] is False for row in rows)


@pytest.mark.asyncio
async def test_events_tail_validation_rejects_bad_window(
    control_plane_url: str,
) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post("/v1/admin/events.tail", json={"since_seconds": 0})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_events_tail_orders_newest_first(
    control_plane_url: str,
    ch_client,
) -> None:
    from loom import Agent

    ns = f"evt-ord-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
            await asyncio.sleep(0.05)
    await _wait_events(ch_client, ns, expected=3)

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post("/v1/admin/events.tail", json={"namespace": ns, "since_seconds": 300})
    rows = r.json()["rows"]
    timestamps = [row["ts"] for row in rows]
    assert timestamps == sorted(timestamps, reverse=True), "events.tail should return newest first"
