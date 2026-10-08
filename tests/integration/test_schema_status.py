"""/v1/health (richer) + /v1/admin/schema.status."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_health_pings_ch(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["ch_ok"] is True
    assert body["schema_version"] == "1"


@pytest.mark.asyncio
async def test_schema_status_no_drift(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/admin/schema.status")
    body = r.json()
    assert body["drift"] is False, (
        f"expected no drift; missing={body['missing']}, extra={body['extra']}"
    )
    # All migrated tables should be present
    for table in [
        "memory",
        "memory_embeddings",
        "audit_log",
        "agent_events",
        "documents",
        "chunks",
        "idempotency",
        "erasure_requests",
        "shared",
    ]:
        assert table in body["present"], f"missing table {table}"
        assert table in body["expected"], f"migration parser missed {table}"


@pytest.mark.asyncio
async def test_schema_status_shape(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/admin/schema.status")
    body = r.json()
    assert isinstance(body["expected"], list)
    assert isinstance(body["present"], list)
    assert isinstance(body["missing"], list)
    assert isinstance(body["extra"], list)
    assert isinstance(body["drift"], bool)
