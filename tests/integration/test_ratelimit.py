"""Per-(namespace, op-class) in-process token bucket rate limiter.

Verifies:
  - burst is allowed up to capacity
  - over-burst returns 429 RATE_LIMITED with Retry-After header
  - waiting Retry-After + small slack restores access
  - different namespaces don't share buckets
  - different op-classes don't share buckets (memory is more generous than knowledge.add)
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest


def _auth_headers() -> dict[str, str]:
    """Tests hit /v1 endpoints with raw httpx — LOOM_AUTH_REQUIRED=true
    is the production default so we have to attach the suite's wildcard
    LOOM_API_KEY (seeded by tests/conftest.py autouse fixture)."""
    tok = os.environ.get("LOOM_API_KEY")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
async def reset_buckets():
    """Clean bucket state before each test so capacity is predictable."""
    from server.core.ratelimit import get_limiter

    get_limiter().reset()
    yield
    get_limiter().reset()


async def _flood(base_url: str, op_path: str, body: dict, count: int) -> list[int]:
    """Fire `count` requests concurrently; return list of status codes.

    Sequential `await` in a loop doesn't reliably trigger rate-limit 429s
    against a remote CH service (per-call RTT exceeds token refill
    interval), so the limiter has time to refill mid-flood. Fire all
    requests in parallel via asyncio.gather so they all hit the in-process
    limiter before tokens refresh.
    """
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0, headers=_auth_headers()) as c:
        responses = await asyncio.gather(*[c.post(op_path, json=body) for _ in range(count)])
    return [r.status_code for r in responses]


@pytest.mark.asyncio
async def test_burst_within_capacity_allowed(control_plane_url: str) -> None:
    """knowledge.add capacity is 10. The first 10 requests in a fresh
    bucket should all succeed."""
    ns = f"rl-{uuid.uuid4().hex[:8]}"
    body = {
        "org": f"test-org-{uuid.uuid4().hex[:6]}",
        "namespace": ns,
        "content": "x",
        "source_uri": "src://1",
        "tags": [],
    }
    statuses = await _flood(control_plane_url, "/v1/knowledge.add", body, 10)
    assert all(s == 200 for s in statuses), f"unexpected non-200 in burst: {statuses}"


@pytest.mark.asyncio
async def test_over_burst_returns_429(control_plane_url: str) -> None:
    """Past the burst capacity, knowledge.add returns 429 with Retry-After."""
    ns = f"rl-{uuid.uuid4().hex[:8]}"
    body = {
        "org": f"test-org-{uuid.uuid4().hex[:6]}",
        "namespace": ns,
        "content": "x",
        "source_uri": "src://x",
        "tags": [],
    }
    # Capacity=10 for knowledge.add; fire 15 in parallel with distinct
    # source_uris. Concurrent firing is required against remote-CH
    # deployments where sequential per-call RTT exceeds the token-refill
    # interval.
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers=_auth_headers()
    ) as c:
        responses = await asyncio.gather(
            *[
                c.post("/v1/knowledge.add", json={**body, "source_uri": f"src://{i}"})
                for i in range(15)
            ]
        )
    statuses = [r.status_code for r in responses]
    retry_afters = [r.headers.get("Retry-After") for r in responses if r.status_code == 429]

    # First several should succeed; later ones should 429
    assert statuses.count(200) >= 5, f"expected some successes: {statuses}"
    assert statuses.count(429) >= 1, f"expected throttling kicked in: {statuses}"
    assert all(ra is not None and int(ra) >= 1 for ra in retry_afters), (
        "Retry-After header missing on 429 responses"
    )

    # The 429 response body should carry the structured code
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers=_auth_headers()
    ) as c:
        responses_2 = await asyncio.gather(
            *[
                c.post("/v1/knowledge.add", json={**body, "source_uri": f"src://x{i}"})
                for i in range(15)
            ]
        )
    throttled = [r for r in responses_2 if r.status_code == 429]
    assert throttled, "could not trigger 429 a second time"
    assert throttled[0].json()["detail"]["code"] == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_different_namespaces_have_independent_buckets(
    control_plane_url: str,
) -> None:
    """One tenant's saturated bucket doesn't throttle another tenant."""
    ns_a = f"rl-a-{uuid.uuid4().hex[:6]}"
    ns_b = f"rl-b-{uuid.uuid4().hex[:6]}"
    body_a = {
        "org": f"test-org-{uuid.uuid4().hex[:6]}",
        "namespace": ns_a,
        "content": "x",
        "source_uri": "s",
        "tags": [],
    }
    body_b = {
        "org": f"test-org-{uuid.uuid4().hex[:6]}",
        "namespace": ns_b,
        "content": "y",
        "source_uri": "s",
        "tags": [],
    }

    # Saturate ns_a's knowledge.add bucket — concurrent so we exhaust
    # tokens before any refill ticks in.
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers=_auth_headers()
    ) as c:
        await asyncio.gather(
            *[
                c.post("/v1/knowledge.add", json={**body_a, "source_uri": f"src-a-{i}"})
                for i in range(15)
            ]
        )
        # ns_b's bucket is untouched; should still succeed
        r = await c.post("/v1/knowledge.add", json={**body_b, "source_uri": "src-b-0"})
    assert r.status_code == 200, "ns_b should not be rate-limited because of ns_a's traffic"


@pytest.mark.asyncio
async def test_memory_class_more_generous_than_knowledge_add(
    control_plane_url: str,
) -> None:
    """memory.* capacity is 100; knowledge.add is 10. 15 memory ops should all succeed."""
    ns = f"rl-mem-{uuid.uuid4().hex[:6]}"
    body = {
        "org": f"test-org-{uuid.uuid4().hex[:6]}",
        "namespace": ns,
        "agent": "t",
        "memory_key": "k",
        "memory_type": "semantic",
        "value": 1,
    }
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=10.0, headers=_auth_headers()
    ) as c:
        responses = await asyncio.gather(*[c.post("/v1/memory.set", json=body) for _ in range(15)])
    statuses = [r.status_code for r in responses]
    assert all(s == 200 for s in statuses), f"memory.set should not throttle at 15 reqs: {statuses}"


@pytest.mark.asyncio
async def test_token_bucket_unit_behavior() -> None:
    """Unit-shaped check on the TokenBucket itself."""

    from server.core.ratelimit import Limit, TokenBucket

    bucket = TokenBucket(Limit(capacity=3, rate=10))
    # Three immediate acquires succeed
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    assert bucket.try_acquire()
    # Fourth fails
    assert not bucket.try_acquire()
    # seconds_until_available > 0
    wait = bucket.seconds_until_available()
    assert 0 < wait < 1.0
    # Wait then try again
    await asyncio.sleep(wait + 0.05)
    assert bucket.try_acquire()
