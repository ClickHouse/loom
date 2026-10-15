"""SDK Retry-After-aware retry on 429.

When pacing is enabled (the default), the SDK catches
`RateLimited`, sleeps for the server's `Retry-After` hint, and retries until
its bounded wait budget (15 s, each sleep capped at 5 s) is spent before
surfacing. With `pacing=False`, 429 surfaces immediately as `RateLimited` so
the caller can handle it themselves.

Composes with the SDK pacing slice: a paced caller now both
preempts 429s when possible AND recovers from them when the burst
exceeds the pre-flight estimate.
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

from tests.integration._eventually import overrides_adopted

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


async def _set_override(
    base_url: str, ns: str, op_class: str, capacity: float, rate: float
) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as c:
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


@pytest.mark.asyncio
async def test_pacing_off_429_surfaces_as_RateLimited(control_plane_url: str) -> None:
    """`pacing=False` (pacing is on by default): a 429
    surfaces as RateLimited immediately."""
    import asyncio

    from loom import Agent
    from loom.errors import RateLimited

    ns = f"ra-off-{uuid.uuid4().hex[:6]}"

    async with Agent(
        org="test", namespace=ns, agent="t", base_url=control_plane_url, pacing=False
    ) as a:
        # Fire concurrently so all 20 hit the limiter before tokens refill
        # (sequential firing against remote CH lets the bucket replenish
        # between calls, masking the 429).
        async def _add(i):
            try:
                await a.knowledge.add(content="x", source_uri=f"s{i}", tags=[])
                return None
            except RateLimited as e:
                return e

        results = await asyncio.gather(*[_add(i) for i in range(20)])
    rate_limited = [r for r in results if isinstance(r, RateLimited)]
    assert rate_limited, "pacing-off should surface RateLimited"
    retry_after = rate_limited[0].retry_after_s
    assert retry_after >= 1, f"retry_after_s should be >=1; got {retry_after}"


@pytest.mark.recovery
@pytest.mark.asyncio
async def test_pacing_on_recovers_from_429(control_plane_url: str) -> None:
    """When pacing=True and we hit a 429 anyway (e.g. the pre-flight
    estimate underestimated), the SDK should honor Retry-After and retry,
    completing the call without surfacing an exception."""
    from loom import Agent

    ns = f"ra-on-{uuid.uuid4().hex[:6]}"

    # Tight per-namespace cap: 1 token, refilling at 2/s.
    await _set_override(control_plane_url, ns, "knowledge.add", capacity=1, rate=2)

    async with Agent(
        org="test", namespace=ns, agent="t", base_url=control_plane_url, pacing=True
    ) as a:
        # First call consumes the only token in the bucket
        await a.knowledge.add(content="x", source_uri="s0", tags=[])
        # Second call gets 429; SDK should retry-after-aware retry and succeed
        t0 = time.monotonic()
        await a.knowledge.add(content="x", source_uri="s1", tags=[])
        elapsed = time.monotonic() - t0
        # Retry-After is at least 1s; total wait should be >0.5s
        assert elapsed > 0.4, f"expected retry-after wait inserted; got {elapsed:.2f}s"


@pytest.mark.slow
@pytest.mark.recovery
@pytest.mark.asyncio
async def test_pacing_on_eventually_surfaces_after_retry_exhausted(
    control_plane_url: str,
) -> None:
    """If 429s keep happening past the SDK's wait budget, RateLimited
    eventually surfaces (no infinite loop)."""
    from loom import Agent
    from loom.errors import RateLimited

    ns = f"ra-exh-{uuid.uuid4().hex[:6]}"
    # cap=1, rate=0.001/s: refill is ~1000s per token. SDK's Retry-After
    # sleep is capped at 5s and the per-call budget at 15s, so 3 retries ×
    # 5s spend the budget and the bucket still hasn't refilled. RateLimited
    # eventually surfaces.
    await _set_override(control_plane_url, ns, "knowledge.add", capacity=1, rate=0.001)

    async with Agent(
        org="test", namespace=ns, agent="t", base_url=control_plane_url, pacing=True
    ) as a:
        # Drain the 1 token in the bucket — this call succeeds.
        await a.knowledge.add(content="x", source_uri="s0", tags=[])
        t0 = time.monotonic()
        with pytest.raises(RateLimited):
            await a.knowledge.add(content="x", source_uri="s1", tags=[])
        elapsed = time.monotonic() - t0
        # 15 s budget / 5 s cap = 3 retries. Refill rate is glacial so
        # every retry still 429s, eventually exhausting and surfacing.
        # Total wall time ≥ ~10s (allowing for some slack).
        assert elapsed > 8.0, f"expected ~15s of retries before exhaustion; got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_RateLimited_is_subclass_of_BackendUnavailable() -> None:
    """Existing callers catching BackendUnavailable still match RateLimited."""
    from loom.errors import BackendUnavailable, RateLimited

    e = RateLimited(retry_after_s=3)
    assert isinstance(e, BackendUnavailable)
    assert e.retry_after_s == 3
    assert "rate-limited" in str(e).lower()
    assert e.code == "RATE_LIMITED"
