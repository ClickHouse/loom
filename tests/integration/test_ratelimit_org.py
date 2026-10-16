"""Org-level rate-limit caps stacked on top of per-namespace caps.

Bounds a noisy tenant whose namespaces collectively exceed reasonable
usage — even when each individual namespace is under its own cap.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration._eventually import overrides_adopted

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def _auth_headers() -> dict[str, str]:
    tok = os.environ.get("LOOM_API_KEY")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


async def _set_ns_override(
    c: httpx.AsyncClient, ns: str, op_class: str, capacity: float, rate: float
) -> None:
    r = await c.post(
        "/v1/admin/ratelimit.set",
        json={
            "namespace": ns,
            "op_class": op_class,
            "capacity": capacity,
            "rate": rate,
        },
    )
    assert r.status_code == 200, r.text
    await overrides_adopted()


async def _set_org_override(
    c: httpx.AsyncClient, org: str, op_class: str, capacity: float, rate: float
) -> None:
    r = await c.post(
        "/v1/admin/ratelimit.set",
        json={
            "org": org,
            "op_class": op_class,
            "capacity": capacity,
            "rate": rate,
        },
    )
    assert r.status_code == 200, r.text
    await overrides_adopted()


@pytest.mark.asyncio
async def test_org_cap_throttles_fan_out_across_namespaces(
    control_plane_url: str,
) -> None:
    """One org with low org cap + two namespaces each within their own ns
    cap: the org cap fires before the ns cap because total exceeds the
    org bucket."""
    org = f"noisy-{uuid.uuid4().hex[:6]}"
    ns_a = f"a-{uuid.uuid4().hex[:6]}"
    ns_b = f"b-{uuid.uuid4().hex[:6]}"

    body = {"content": "x", "tags": []}
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers=_auth_headers()
    ) as c:
        # Tight org cap: 5 capacity, slow refill.
        # Per-ns default for knowledge.add is 10 — each ns alone would
        # serve 10 reqs, but the org-bucket binds first at 5.
        await _set_org_override(c, org, "knowledge.add", capacity=5, rate=1)

        # Concurrent firing so all 10 hit the limiter before refill ticks.
        responses = await asyncio.gather(
            *[
                c.post(
                    "/v1/knowledge.add",
                    json={
                        **body,
                        "org": org,
                        "namespace": ns_a if i % 2 == 0 else ns_b,
                        "source_uri": f"s-{i}",
                    },
                )
                for i in range(10)
            ]
        )
    statuses = [r.status_code for r in responses]

    # 5 should succeed (org bucket capacity), the rest 429
    assert statuses.count(200) == 5, f"org-bucket should serve exactly its 5-cap; got {statuses}"
    assert statuses.count(429) == 5


@pytest.mark.asyncio
async def test_sibling_org_unaffected_by_noisy_org(control_plane_url: str) -> None:
    """An org cap is tenant-scoped: another org still gets its own budget."""
    noisy = f"noisy-{uuid.uuid4().hex[:6]}"
    quiet = f"quiet-{uuid.uuid4().hex[:6]}"
    ns_n = f"n-{uuid.uuid4().hex[:6]}"
    ns_q = f"q-{uuid.uuid4().hex[:6]}"

    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=5.0, headers=_auth_headers()
    ) as c:
        await _set_org_override(c, noisy, "knowledge.add", capacity=2, rate=0.001)
        # Drain the noisy org's bucket entirely
        for i in range(5):
            await c.post(
                "/v1/knowledge.add",
                json={
                    "org": noisy,
                    "namespace": ns_n,
                    "content": "x",
                    "source_uri": f"n-{i}",
                    "tags": [],
                },
            )
        # quiet org still has its default org cap (30) — should succeed
        r = await c.post(
            "/v1/knowledge.add",
            json={
                "org": quiet,
                "namespace": ns_q,
                "content": "x",
                "source_uri": "q-0",
                "tags": [],
            },
        )
    assert r.status_code == 200, (
        f"quiet org should be unaffected by noisy org's saturation: {r.text}"
    )


@pytest.mark.asyncio
async def test_ns_cap_still_applies_under_loose_org_cap(
    control_plane_url: str,
) -> None:
    """Generous org cap + tight ns cap: the ns is the binding limit."""
    org = f"big-{uuid.uuid4().hex[:6]}"
    ns = f"tight-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers=_auth_headers()
    ) as c:
        await _set_org_override(c, org, "knowledge.add", capacity=1000, rate=100)
        await _set_ns_override(c, ns, "knowledge.add", capacity=3, rate=0.5)
        responses = await asyncio.gather(
            *[
                c.post(
                    "/v1/knowledge.add",
                    json={
                        "org": org,
                        "namespace": ns,
                        "content": "x",
                        "source_uri": f"s-{i}",
                        "tags": [],
                    },
                )
                for i in range(8)
            ]
        )
    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == 3, (
        f"ns bucket caps at 3 even under a loose org cap; got {statuses}"
    )
    assert 429 in statuses


@pytest.mark.asyncio
async def test_clear_org_override(control_plane_url: str) -> None:
    """clear: true on an org-scoped payload removes the override."""
    org = f"clearorg-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=5.0, headers=_auth_headers()
    ) as c:
        await _set_org_override(c, org, "knowledge.add", capacity=2, rate=0.5)
        r = await c.post(
            "/v1/admin/ratelimit.set",
            json={
                "org": org,
                "op_class": "knowledge.add",
                "clear": True,
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] is True
    assert body["scope"] == "org"
    assert body["key"] == org


@pytest.mark.asyncio
async def test_validation_rejects_both_scopes(control_plane_url: str) -> None:
    """Sending both `org` and `namespace` is ambiguous — must 422."""
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=5.0, headers=_auth_headers()
    ) as c:
        r = await c.post(
            "/v1/admin/ratelimit.set",
            json={
                "org": "o",
                "namespace": "n",
                "op_class": "memory",
                "capacity": 10,
                "rate": 2,
            },
        )
    assert r.status_code == 422
    assert "namespace" in r.text or "org" in r.text


@pytest.mark.asyncio
async def test_429_carries_headers_from_binding_bucket(
    control_plane_url: str,
) -> None:
    """When the org bucket binds (tighter than ns), the 429 response's
    X-RateLimit-Limit should reflect the org bucket's capacity."""
    org = f"hdrtight-{uuid.uuid4().hex[:6]}"
    ns = f"hdr-{uuid.uuid4().hex[:6]}"
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=5.0, headers=_auth_headers()
    ) as c:
        await _set_org_override(c, org, "knowledge.add", capacity=2, rate=0.5)
        await _set_ns_override(c, ns, "knowledge.add", capacity=100, rate=10)
        # Drain org bucket
        for i in range(2):
            await c.post(
                "/v1/knowledge.add",
                json={
                    "org": org,
                    "namespace": ns,
                    "content": "x",
                    "source_uri": f"d-{i}",
                    "tags": [],
                },
            )
        # Next req: 429 because the org bucket binds
        r = await c.post(
            "/v1/knowledge.add",
            json={
                "org": org,
                "namespace": ns,
                "content": "x",
                "source_uri": "third",
                "tags": [],
            },
        )
    assert r.status_code == 429
    # Header reflects the org (binding) bucket, not the loose ns one
    assert r.headers["X-RateLimit-Limit"] == "2", (
        f"expected org-bucket cap=2; got {r.headers['X-RateLimit-Limit']}"
    )
