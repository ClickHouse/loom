"""Every process that verifies tokens loads and refreshes its own revocation
snapshot and signing keyring, whether or not it holds the supervisor lease, or
a revoked token keeps authenticating until a restart."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.auth import revocation
from server.core.ch import current_query_tag
from server.settings import settings

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


@pytest.mark.asyncio
async def test_token_state_loads_each_at_boot_and_runs_each_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server import boot
    from server.auth import end_user_keys, keyring
    from server.core import grant_cache

    loaded: list[tuple[str, str]] = []

    async def _load(name: str) -> None:
        loaded.append((name, current_query_tag()))

    async def _forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(revocation, "load_at_boot", lambda: _load("revocation"))
    monkeypatch.setattr(keyring, "refresh", lambda: _load("keyring"))
    monkeypatch.setattr(
        revocation, "start_refresh_loop", lambda _ttl: asyncio.create_task(_forever(), name="r")
    )
    monkeypatch.setattr(
        keyring, "start_refresh_loop", lambda: asyncio.create_task(_forever(), name="k")
    )
    monkeypatch.setattr(grant_cache, "poll", lambda: _load("grants"))
    monkeypatch.setattr(end_user_keys, "refresh", lambda: _load("end-user keys"))
    monkeypatch.setattr(
        end_user_keys, "start_refresh_loop", lambda: asyncio.create_task(_forever(), name="e")
    )
    periods: list[float] = []

    def _poll_loop(period: float) -> asyncio.Task[None]:
        periods.append(period)
        return asyncio.create_task(_forever(), name="g")

    monkeypatch.setattr(grant_cache, "start_poll_loop", _poll_loop)
    monkeypatch.setattr(settings, "acl_watermark_poll_seconds", 0.5)
    loops = await boot.start_token_state()
    # Each boot load is tagged as its refresh loop is, and only for its call.
    assert loaded == [
        ("revocation", "revocation.refresh"),
        ("keyring", "auth.keyring_refresh"),
        ("grants", "grant_cache.poll"),
        ("end-user keys", ""),
    ]
    assert current_query_tag() == ""
    assert [t.get_name() for t in loops] == ["r", "k", "e", "g"]
    assert periods == [0.5]
    for t in loops:
        t.cancel()


def test_lifespan_starts_the_token_state_and_cancels_it_at_shutdown() -> None:
    src = (ROOT / "server" / "main.py").read_text()
    lifespan = src[src.index("async def lifespan(") :]
    assert "auth_loops = await boot.start_token_state()" in lifespan
    assert (
        "for loop in (*cell_loops, *auth_loops, overrides_loop):\n            loop.cancel()"
        in lifespan
    )
    # the decorator still sits on lifespan, not on the helper above it
    assert "@asynccontextmanager\nasync def lifespan(" in src


@pytest.mark.asyncio
async def test_refresh_loop_tags_its_queries_as_itself_not_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.ch import current_query_tag

    tags: list[str] = []

    async def _fake_refresh() -> None:
        tags.append(current_query_tag())

    monkeypatch.setattr(revocation, "refresh", _fake_refresh)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(revocation.asyncio, "sleep", lambda s: real_sleep(0.001))
    task = revocation.start_refresh_loop(1)
    for _ in range(500):
        if tags:
            break
        await real_sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert tags and tags[0] == "revocation.refresh"
