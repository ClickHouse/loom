"""CORS middleware — opt-in via LOOM_CORS_ORIGINS."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from tests.integration._spawn import loom_server


@pytest.fixture(scope="module")
def cors_server() -> Iterator[str]:
    """A separate server with LOOM_CORS_ORIGINS set; the suite-wide server
    keeps its configuration. CORS handling holds no state, so one server
    serves the module."""
    env = {"LOOM_CORS_ORIGINS": "https://ops.example.com,https://dash.local"}
    with loom_server("cors", env, log_level="info") as base_url:
        yield base_url


@pytest.mark.asyncio
async def test_cors_preflight_allowed_origin(cors_server: str) -> None:
    """OPTIONS preflight from an allowed origin gets CORS headers back."""
    async with httpx.AsyncClient(base_url=cors_server, timeout=5.0) as c:
        r = await c.options(
            "/v1/memory.set",
            headers={
                "Origin": "https://ops.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type, Authorization",
            },
        )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://ops.example.com"
    assert "POST" in r.headers["access-control-allow-methods"]


@pytest.mark.asyncio
async def test_cors_get_includes_origin_header(cors_server: str) -> None:
    """A GET from an allowed origin gets Access-Control-Allow-Origin echoed."""
    async with httpx.AsyncClient(base_url=cors_server, timeout=5.0) as c:
        r = await c.get(
            "/v1/health",
            headers={
                "Origin": "https://dash.local",
            },
        )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://dash.local"


@pytest.mark.asyncio
async def test_cors_rejects_unlisted_origin(cors_server: str) -> None:
    """An origin not in the allow-list gets no CORS-allow header. With auth
    off the cross-site gate refuses it outright."""
    async with httpx.AsyncClient(base_url=cors_server, timeout=5.0) as c:
        r = await c.get(
            "/v1/health",
            headers={
                "Origin": "https://evil.example.com",
            },
        )
    if r.status_code == 403:
        assert r.json()["detail"]["code"] == "CROSS_SITE_REQUEST"
    else:
        assert r.status_code == 200
    # FastAPI / Starlette's CORSMiddleware omits the header when origin
    # is not allowed.
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.asyncio
async def test_cors_disabled_by_default(control_plane_url: str) -> None:
    """The suite-wide server (no LOOM_CORS_ORIGINS) has no CORS headers."""
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=5.0) as c:
        r = await c.get("/v1/health", headers={"Origin": "https://anywhere"})
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.asyncio
async def test_cors_exposes_rate_limit_headers(cors_server: str) -> None:
    """X-RateLimit-* headers are in expose_headers so JS can read them."""
    async with httpx.AsyncClient(base_url=cors_server, timeout=5.0) as c:
        await c.options(
            "/v1/memory.set",
            headers={
                "Origin": "https://ops.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
    # Browsers read expose headers from a successful response, not preflight,
    # but the explicit Access-Control-Expose-Headers is set per-response in
    # FastAPI's CORSMiddleware via expose_headers config.
    # Verify by making an actual request and checking
    async with httpx.AsyncClient(base_url=cors_server, timeout=5.0) as c:
        r2 = await c.get(
            "/v1/health",
            headers={
                "Origin": "https://ops.example.com",
            },
        )
    exposed = r2.headers.get("access-control-expose-headers", "")
    assert "X-RateLimit-Limit" in exposed
    assert "Retry-After" in exposed
