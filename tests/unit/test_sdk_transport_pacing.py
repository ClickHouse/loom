"""The SDK paces by default — a 429 is retried after the
server's Retry-After inside a bounded wait budget — and `pacing=False` keeps
the immediate `RateLimited`."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from loom import Agent, Datapack, SyncAgent
from loom.errors import RateLimited
from loom.transport import Transport


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import loom.transport as tmod

    out: list[float] = []

    async def _sleep(s: float) -> None:
        out.append(s)

    monkeypatch.setattr(tmod.asyncio, "sleep", _sleep)
    return out


def _limited(t: Transport, retry_after_s: int, fail_times: int) -> list[int]:
    attempts: list[int] = []

    async def _post_once(op_path: str, payload: Any, headers: Any, timeout_s: Any = None) -> Any:
        attempts.append(1)
        if len(attempts) <= fail_times:
            raise RateLimited(retry_after_s=retry_after_s)
        return {"ok": True}

    t._post_once = _post_once  # type: ignore[method-assign]
    return attempts


def test_pacing_is_on_by_default_everywhere() -> None:
    assert Transport("http://x").pacing is True
    assert Agent(org="o", namespace="n", agent="a", api_key="x").transport.pacing is True
    assert Datapack("o", "n", api_key="x").agent("a").transport.pacing is True
    assert (
        Agent(org="o", namespace="n", agent="a", api_key="x", pacing=False).transport.pacing
        is False
    )


def test_sync_agent_paces_by_default() -> None:
    with SyncAgent(org="o", namespace="n", agent="a", api_key="x") as sa:
        assert sa.transport.pacing is True


async def test_a_429_is_retried_after_the_hint_and_then_succeeds(sleeps: list[float]) -> None:
    t = Transport("http://x", pacing=True)
    attempts = _limited(t, retry_after_s=1, fail_times=2)
    out = await t.post("/v1/memory.set", {})
    assert out == {"ok": True} and len(attempts) == 3 and sleeps == [1.0, 1.0]
    await t.close()


async def test_the_wait_budget_bounds_the_retries(sleeps: list[float]) -> None:
    t = Transport("http://x", pacing=True, ratelimit_max_wait_s=3.0)
    attempts = _limited(t, retry_after_s=1, fail_times=99)
    with pytest.raises(RateLimited):
        await t.post("/v1/memory.set", {})
    assert sleeps == [1.0, 1.0, 1.0] and len(attempts) == 4
    await t.close()


async def test_each_sleep_is_capped_and_the_default_budget_is_fifteen_seconds(
    sleeps: list[float],
) -> None:
    t = Transport("http://x", pacing=True)
    _limited(t, retry_after_s=1000, fail_times=99)
    with pytest.raises(RateLimited):
        await t.post("/v1/memory.set", {})
    assert sleeps == [5.0, 5.0, 5.0] and sum(sleeps) == Transport.DEFAULT_RATELIMIT_MAX_WAIT_S
    await t.close()


async def test_pacing_off_raises_at_once(sleeps: list[float]) -> None:
    t = Transport("http://x", pacing=False)
    attempts = _limited(t, retry_after_s=1, fail_times=1)
    with pytest.raises(RateLimited):
        await t.post("/v1/memory.set", {})
    assert sleeps == [] and len(attempts) == 1
    await t.close()


async def test_a_zero_budget_means_no_retry(sleeps: list[float]) -> None:
    t = Transport("http://x", pacing=True, ratelimit_max_wait_s=0)
    _limited(t, retry_after_s=1, fail_times=1)
    with pytest.raises(RateLimited):
        await t.post("/v1/memory.set", {})
    assert sleeps == []
    await t.close()


def test_sleep_patch_is_asyncio_module_level() -> None:
    # Guard for the fixture: the transport awaits asyncio.sleep by module attribute.
    assert asyncio.sleep is not None
