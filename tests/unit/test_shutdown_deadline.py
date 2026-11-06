"""The last of a shutdown (`server.boot.leave_deployment`) fits its deadline whatever
stalls, and the process exits right after it: Docker's stop grace kills a process
that waits on a stalled settle, a client close or the heartbeat's thread."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from server import boot
from server.core import rmt_carry
from server.insights.app import provider_barrier as barrier

ROOT = Path(__file__).resolve().parents[2]


class _StalledCH:
    """Every statement hangs: a KILL QUERY … SYNC, the acknowledgement insert, the close."""

    def __init__(self) -> None:
        self.inserted: list[Any] = []
        self.closed = False

    async def execute(self, *_a: Any, **_k: Any) -> None:
        await asyncio.sleep(30)

    async def query_all(self, *_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(30)

    async def insert(self, table: str, rows: Any, **_k: Any) -> None:
        self.inserted.append(rows)
        await asyncio.sleep(30)

    async def disconnect(self) -> None:
        self.closed = True
        await asyncio.sleep(30)


@pytest.fixture
def _cut_off_write(monkeypatch: pytest.MonkeyPatch):
    rmt_carry.reset_for_tests()
    rmt_carry._published = ("{}", time.monotonic())
    pin = rmt_carry._Pin(rmt_carry._gen, ["q-cut"], time.time() + 60, time.time() + 120)
    rmt_carry._pinned.append(pin)
    rmt_carry._inflight[pin.gen] += 1
    yield
    rmt_carry.reset_for_tests()


async def _not_settled() -> bool:
    return False


@pytest.mark.usefixtures("_cut_off_write")
async def test_retirement_then_withdrawal_then_close_share_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(barrier, "retire_process", _not_settled)
    ch = _StalledCH()
    started = time.monotonic()
    await boot.leave_deployment(ch, budget_s=0.3)
    assert time.monotonic() - started < 1.0
    assert ch.inserted == [], "the acknowledgement is left to expire, not withdrawn"
    assert ch.closed


async def test_a_stalled_instance_stop_write_still_leaves_time_for_the_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse stops answering at SIGTERM: the instance row's stop write is cut off
    at its own bound, inside the drains' budget, and the drains and the final cleanup
    still run."""
    from server import main
    from server.core import instances

    ran: list[str] = []

    async def _drains(*_a: Any) -> None:
        ran.append("drains")

    async def _leave(ch: Any, *, budget_s: float) -> None:
        ran.append(f"leave {budget_s:.1f}")

    monkeypatch.setattr(instances, "STOP_S", 0.1)
    monkeypatch.setattr(main, "_shutdown_drains", _drains)
    monkeypatch.setattr(boot, "leave_deployment", _leave)
    monkeypatch.setattr(boot, "SHUTDOWN_DRAIN_BUDGET_S", 1.0)
    started = time.monotonic()
    await main._shut_down(_StalledCH(), None, None, None)
    assert time.monotonic() - started < 0.6
    assert ran[0] == "drains" and ran[1].startswith("leave ")
    assert float(ran[1].split()[1]) > barrier.SHUTDOWN_RETIRE_S  # the unused drain time


_EXIT = """
import asyncio, atexit, sys, time
from unittest.mock import AsyncMock
from server.insights.app import provider_barrier as barrier

class _Hangs:
    calls = 0
    def query(self, *a, **k):
        self.calls += 1
        if self.calls > 1:
            time.sleep(10)  # a beat stuck on a ClickHouse that stopped answering
        return type("R", (), {"result_rows": [(0,)]})()
    def insert(self, *a, **k): pass
    def close(self): pass

barrier.HEARTBEAT_S = 0.05
beat = barrier._Heartbeat(lambda: _Hangs())
barrier._HEARTBEAT = beat
beat.start()
time.sleep(0.3)  # the second beat is now stuck
barrier.drain_task_sets = AsyncMock(return_value=True)
barrier._server_side_settled = AsyncMock(return_value=True)
barrier.record_participant = AsyncMock()
started = time.monotonic()
# atexit handlers run after the interpreter joins its non-daemon threads, so
# this is the time from retiring to the process's exit, imports left out.
atexit.register(lambda: print(f"{time.monotonic() - started:.2f}"))
assert asyncio.run(barrier.retire_process())
"""


def test_the_process_exits_without_waiting_on_a_stuck_heartbeat() -> None:
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_EXIT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert float(done.stdout.split()[-1]) < 5, "exit waited on the heartbeat thread"
