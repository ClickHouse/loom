"""One supervisor per host: the file-lock lease that lets
`uvicorn --workers N` run N web processes and exactly one background
supervisor, and the rate limiter's awareness that N workers are N replicas."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from server.workers.leader import SupervisorLease, decide_supervisor


def test_first_lease_wins_and_a_sibling_is_refused(tmp_path) -> None:
    path = str(tmp_path / "loom-supervisor.lock")
    a = SupervisorLease(path=path)
    b = SupervisorLease(path=path)
    assert a.acquire() is True and a.held and "holds" in a.reason
    # flock is per open file description: a second fd on the same path in the
    # same process conflicts exactly as a sibling process would.
    assert b.acquire() is False and not b.held and "another process" in b.reason
    a.release()
    assert not a.held
    assert b.acquire() is True  # the lock outlives nothing: released → next taker wins
    b.release()


def test_lock_is_dropped_when_the_holder_exits(tmp_path) -> None:
    """The kernel releases the lock with the process; the respawned worker
    acquires it on its next boot without any lease expiry logic."""
    path = str(tmp_path / "lock")
    code = (
        f"import sys; sys.path.insert(0, {os.getcwd()!r}); "
        "from server.workers.leader import SupervisorLease; "
        f"lease = SupervisorLease(path={path!r}); print(lease.acquire())"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "True", out.stderr
    assert SupervisorLease(path=path).acquire() is True


def test_modes_on_and_off_do_not_touch_the_lock(tmp_path) -> None:
    path = str(tmp_path / "lock")
    on = decide_supervisor("on", path)
    assert on.held and on.reason == "LOOM_BACKGROUND_WORKERS=on"
    off = decide_supervisor("off", path)
    assert not off.held and off.reason == "LOOM_BACKGROUND_WORKERS=off"
    assert not os.path.exists(path)  # neither mode created the lock file


def test_auto_mode_uses_the_lock(tmp_path) -> None:
    path = str(tmp_path / "lock")
    first = decide_supervisor("auto", path)
    second = decide_supervisor("AUTO", path)
    assert first.held and not second.held
    first.release()


def test_unwritable_lock_path_fails_open(tmp_path) -> None:
    """A host with no writable lock path must not end up with zero supervisors."""
    lease = SupervisorLease(path=str(tmp_path / "no" / "such" / "dir" / "lock"))
    assert lease.acquire() is True and "lock path unavailable" in lease.reason


def test_settings_defaults_keep_todays_behaviour() -> None:
    from server.settings import Settings

    s = Settings()
    assert s.web_workers == 1
    assert s.background_workers == "auto"
    assert s.supervisor_lock_path.endswith("loom-supervisor.lock")


def test_rate_limiter_counts_web_workers_as_replicas(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import ratelimit
    from server.settings import settings

    monkeypatch.setattr(settings, "replica_count", 2)
    monkeypatch.setattr(settings, "web_workers", 4)
    assert ratelimit.effective_replicas() == 8
    monkeypatch.setattr(settings, "web_workers", 0)
    assert ratelimit.effective_replicas() == 2  # 0/unset = one process


# ---- lease retry --------------------------------------------


async def test_retry_acquires_when_the_holder_releases_and_starts_once(tmp_path) -> None:
    """The new colour of a blue/green deploy boots while the old colour holds
    the lease; when the old one exits the retry loop takes it and starts the
    supervisor — once."""
    import asyncio

    from server.workers.leader import retry_until_held

    path = str(tmp_path / "lock")
    holder = SupervisorLease(path=path)
    assert holder.acquire()
    waiter = decide_supervisor("auto", path)
    assert not waiter.held
    started: list[int] = []
    task = asyncio.create_task(retry_until_held(waiter, 0.05, lambda: started.append(1)))
    await asyncio.sleep(0.2)
    assert not waiter.held and started == []  # still held by the other process
    holder.release()
    await asyncio.wait_for(task, timeout=5)
    assert waiter.held and "holds" in waiter.reason and started == [1]
    waiter.release()


async def test_retry_is_off_at_interval_zero_and_a_held_lease_returns_at_once(tmp_path) -> None:
    from server.workers.leader import retry_until_held

    path = str(tmp_path / "lock")
    holder = SupervisorLease(path=path)
    assert holder.acquire()
    waiter = SupervisorLease(path=path)
    assert not waiter.acquire()
    started: list[int] = []
    await retry_until_held(waiter, 0.0, lambda: started.append(1))  # disabled: returns
    assert not waiter.held and started == []
    await retry_until_held(holder, 0.05, lambda: started.append(2))  # already held: no-op
    assert started == []
    holder.release()
