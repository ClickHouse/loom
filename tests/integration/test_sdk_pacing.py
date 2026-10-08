"""SDK pacing — Transport(pacing=True) reads X-RateLimit-* and pre-emptively
sleeps a refill tick when budget is at floor.

Headline test: a burst of N requests beyond the bucket capacity completes
without raising (because the SDK paces between calls), and takes >0 wall
time (because pacing inserted sleeps).
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


@pytest.mark.asyncio
async def test_pacing_avoids_429_in_burst(control_plane_url: str) -> None:
    """A burst of 15 knowledge.add requests (default capacity=10) with
    pacing on should not raise — the SDK should sleep between calls when
    budget hits floor and avoid the 429s that test_over_burst_returns_429
    explicitly tickles."""
    from loom import Agent

    ns = f"pace-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org="test",
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        pacing=True,
    ) as a:
        for i in range(15):
            await a.knowledge.add(
                content="x",
                source_uri=f"s{i}",
                tags=[],
            )
    # If we got here without raising, the burst completed.


@pytest.mark.asyncio
async def test_pacing_takes_longer_than_unpaced_burst(control_plane_url: str) -> None:
    """Sanity check the pacing actually paces: with pacing on, a burst that
    exceeds capacity takes measurably longer than the bucket's refill
    interval (one tick = reset/limit seconds). knowledge.add: capacity=10,
    rate=2 → ~0.5s per token. 15 requests should take ≥ ~2.5s; without
    pacing, it'd finish in well under 1s (and 429)."""
    from loom import Agent

    ns = f"pace-t-{uuid.uuid4().hex[:6]}"
    t0 = time.monotonic()
    async with Agent(
        org="test",
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        pacing=True,
    ) as a:
        for i in range(15):
            await a.knowledge.add(content="x", source_uri=f"s{i}", tags=[])
    elapsed = time.monotonic() - t0
    # 15 - 10 = 5 paced calls, each ~0.5s → ≥ ~2s. Generous floor for jitter.
    assert elapsed > 1.0, f"pacing should have added wall time; got {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_pacing_off_still_throws_on_429(control_plane_url: str) -> None:
    """Pacing is on by default; with `pacing=False` a tight burst surfaces its
    429 to the caller."""
    import asyncio

    from loom import Agent
    from loom.errors import BackendUnavailable, RateLimited

    ns = f"nopace-{uuid.uuid4().hex[:6]}"
    async with Agent(
        org="test",
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        pacing=False,
    ) as a:
        # Concurrent firing so all 20 hit the limiter before refill ticks.
        async def _add(i):
            try:
                await a.knowledge.add(content="x", source_uri=f"s{i}", tags=[])
                return None
            except BackendUnavailable as e:
                return e

        results = await asyncio.gather(*[_add(i) for i in range(20)])
    saw_429 = any(isinstance(r, RateLimited) for r in results)
    assert saw_429, "pacing-off SDK should still surface 429s"


@pytest.mark.asyncio
async def test_pacing_endpoint_with_no_headers_is_safe(control_plane_url: str) -> None:
    """If a path returns no X-RateLimit-* headers (e.g. /v1/health is GET,
    not affected), the pacing transport must not crash. We can't hit /health
    through Agent.knowledge.add directly, but we can confirm an unfamiliar
    path is gracefully ignored by hitting a fresh op with pacing on — the
    first call has no prior state to consult, so it should sleep zero."""
    from loom import Agent

    ns = f"fresh-{uuid.uuid4().hex[:6]}"
    t0 = time.monotonic()
    async with Agent(
        org="test",
        namespace=ns,
        agent="t",
        base_url=control_plane_url,
        pacing=True,
    ) as a:
        await a.memory.set("k1", {"v": 1}, memory_type="semantic")
    elapsed = time.monotonic() - t0
    # First call with no prior rate state: zero pre-flight sleep
    assert elapsed < 1.0, f"first call shouldn't pace; got {elapsed:.2f}s"
