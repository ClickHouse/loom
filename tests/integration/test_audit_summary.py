"""POST /v1/admin/audit.summary — event-type aggregation for compliance."""

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


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


async def _wait_for(
    ch_client, org: str, event_type: str, min_count: int = 1, timeout_s: float = 3.0
) -> int:
    deadline = asyncio.get_event_loop().time() + timeout_s
    last = 0
    while asyncio.get_event_loop().time() < deadline:
        rows = ch_client.query(
            """
            SELECT count() FROM loom.audit_log
            WHERE org = {org:String} AND event_type = {et:String}
            """,
            parameters={"org": org, "et": event_type},
        ).result_rows
        last = int(rows[0][0])
        if last >= min_count:
            return last
        await asyncio.sleep(0.1)
    return last


@pytest.mark.asyncio
async def test_summary_counts_by_event_type(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    org = f"audsum-{uuid.uuid4().hex[:6]}"
    ns = f"as-{uuid.uuid4().hex[:6]}"
    async with Agent(org=org, namespace=ns, agent="w", base_url=control_plane_url) as a:
        # memory.delete is audited
        for i in range(3):
            await a.memory.set(f"k{i}", {"v": i}, memory_type="semantic")
            await a.memory.delete(f"k{i}")
    await _wait_for(ch_client, org, "memory.delete", min_count=3)

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post("/v1/admin/audit.summary", json={"org": org, "since_seconds": 300})
    body = r.json()
    by_type = {row["event_type"]: row for row in body["rows"]}
    assert by_type["memory.delete"]["count"] == 3
    assert body["total"] >= 3


@pytest.mark.asyncio
async def test_summary_namespace_filter(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    org = f"audsum-ns-{uuid.uuid4().hex[:6]}"
    keep = f"keep-{uuid.uuid4().hex[:6]}"
    drop = f"drop-{uuid.uuid4().hex[:6]}"

    async with Agent(org=org, namespace=keep, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
        await a.memory.delete("k")
    async with Agent(org=org, namespace=drop, agent="w", base_url=control_plane_url) as a:
        await a.memory.set("k", {"v": 1}, memory_type="semantic")
        await a.memory.delete("k")
    await _wait_for(ch_client, org, "memory.delete", min_count=2)

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post(
            "/v1/admin/audit.summary",
            json={"org": org, "namespace": keep, "since_seconds": 300},
        )
    body = r.json()
    # Only keep's deletion is counted
    delete_count = next(
        (row["count"] for row in body["rows"] if row["event_type"] == "memory.delete"),
        0,
    )
    assert delete_count == 1


@pytest.mark.asyncio
async def test_summary_empty_returns_no_rows(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post(
            "/v1/admin/audit.summary",
            json={"org": f"empty-{uuid.uuid4().hex[:6]}", "since_seconds": 60},
        )
    body = r.json()
    assert body["rows"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_summary_validation_rejects_bad_window(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.post("/v1/admin/audit.summary", json={"since_seconds": 0})
    assert r.status_code == 422
