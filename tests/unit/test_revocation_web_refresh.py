"""Every process that verifies tokens loads and refreshes
its own revocation snapshot — the web tier under LOOM_BACKGROUND_WORKERS=off
included, or a revoked token keeps authenticating until a restart."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from server.auth import revocation

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_refresh_loop_refreshes_on_its_period_until_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def _fake_refresh() -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(revocation, "refresh", _fake_refresh)
    # the period floor is 1 s; shrink sleep so the test runs in milliseconds
    real_sleep = asyncio.sleep
    monkeypatch.setattr(revocation.asyncio, "sleep", lambda s: real_sleep(0.01))
    task = revocation.start_refresh_loop(0.001)
    assert task.get_name() == "loom-revocation-refresh"
    for _ in range(500):  # a count, not a wall-clock window: a loaded runner is slow, not wrong
        if calls >= 3:
            break
        await real_sleep(0.01)
    assert calls >= 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    seen = calls
    await real_sleep(0.03)
    assert calls == seen  # nothing runs after cancellation


@pytest.mark.asyncio
async def test_refresh_loop_survives_a_failing_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def _boom() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("ch down")

    monkeypatch.setattr(revocation, "refresh", _boom)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(revocation.asyncio, "sleep", lambda s: real_sleep(0.01))
    task = revocation.start_refresh_loop(1)
    for _ in range(500):
        if calls >= 2:
            break
        await real_sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2  # kept going past the first failure


@pytest.mark.asyncio
async def test_load_at_boot_fills_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    class _CH:
        async def query_all(self, sql: str, *a: object, **k: object) -> list[dict[str, object]]:
            return [{"jti": "dead-beef", "issued_before": None}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    await revocation.load_at_boot()
    assert revocation.is_revoked("dead-beef")


def test_lifespan_loads_at_boot_and_runs_the_loop_when_the_lease_is_not_held() -> None:
    src = (ROOT / "server" / "main.py").read_text()
    boot_src = (ROOT / "server" / "boot.py").read_text()
    boot = boot_src[
        boot_src.index("async def start_revocation(") : boot_src.index("def start_lease_retry(")
    ]
    assert "await revocation.load_at_boot()" in boot
    assert re.search(
        r"if lease_held:\n\s+return None\n\s+return revocation\.start_refresh_loop\(", boot
    )
    lifespan = src[src.index("async def lifespan(") :]
    assert "revocation_loop = await boot.start_revocation(lease.held)" in lifespan
    # cancelled when the lease is acquired (the worker takes over) and at shutdown
    assert boot_src.count("revocation_loop.cancel()") == 1
    assert src.count("revocation_loop.cancel()") == 1
    # the decorator still sits on lifespan, not on the helper above it
    assert "@asynccontextmanager\nasync def lifespan(" in src
