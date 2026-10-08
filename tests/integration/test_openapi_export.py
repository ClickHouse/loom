"""/v1/api/openapi.json — versioned OpenAPI export for SDK codegen."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_openapi_export_returns_valid_spec(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/api/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert spec["openapi"].startswith("3.")
    assert spec["info"]["title"] == "Loom Control Plane"
    # Spot-check a few op paths are present
    paths = spec["paths"]
    for op_path in [
        "/v1/memory.set",
        "/v1/memory.get",
        "/v1/memory.bulk_set",
        "/v1/knowledge.add",
        "/v1/knowledge.bulk_add",
        "/v1/knowledge.get",
        "/v1/erasure.request",
        "/v1/erasure.export",
        "/v1/cost.summary",
        "/v1/audit.list",
        "/v1/namespace.register",
        "/v1/memory.touch",
    ]:
        assert op_path in paths, f"missing operation path {op_path}"


@pytest.mark.asyncio
async def test_openapi_matches_unversioned(control_plane_url: str) -> None:
    """/v1/api/openapi.json mirrors FastAPI's /openapi.json today;
    the value is in path stability, not content divergence."""
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        v1 = (await c.get("/v1/api/openapi.json")).json()
        live = (await c.get("/openapi.json")).json()
    # Both contain the same operation set
    assert set(v1["paths"]) == set(live["paths"])
