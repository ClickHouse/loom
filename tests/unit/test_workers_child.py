"""The lease holder runs its workers in a child process, which reads the lease
deadline from its parent and is gone by that deadline plus the drain time."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from server.workers import child

# A stand-in child: writes the first deadline it reads to a file, then waits
# for SIGTERM.
_STUB = """
import os, signal, sys, time
fd, out = int(sys.argv[1]), sys.argv[2]
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
line = os.fdopen(fd, "rb").readline()
open(out, "w").write(line.decode())
time.sleep(60)
"""

# A stand-in child that ignores SIGTERM; it touches its file once it does.
_STUBBORN = """
import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).touch()
time.sleep(60)
"""


def _stubborn(monkeypatch, tmp_path):
    """Run `_STUBBORN` as the child; returns its ready file."""
    ready = tmp_path / "ready"
    monkeypatch.setattr(
        child, "child_argv", lambda fd, _alive_fd: [sys.executable, "-c", _STUBBORN, str(ready)]
    )
    return ready


class _Lease:
    def __init__(self, remaining: float = 12.0) -> None:
        self.remaining = remaining
        self.given_up: list[str] = []

    def remaining_s(self) -> float:
        return self.remaining

    def fresh(self) -> bool:
        return self.remaining > 0

    def give_up(self, why: str) -> None:
        self.given_up.append(why)


async def _until(cond) -> None:
    for _ in range(250):
        if cond():
            return
        await asyncio.sleep(0.02)
    assert cond()


def _open_fds() -> set[str] | None:
    return set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None


def _alive(workers: child.ChildWorkers) -> bool:
    return bool(workers.status()[0]["task_alive"])


@pytest.mark.asyncio
async def test_parent_sends_the_deadline_and_stops_the_child(monkeypatch, tmp_path) -> None:
    out = tmp_path / "deadline"
    monkeypatch.setattr(
        child, "child_argv", lambda fd, _alive_fd: [sys.executable, "-c", _STUB, str(fd), str(out)]
    )
    workers = child.ChildWorkers(_Lease())
    workers.start_all()
    await _until(lambda: out.exists() and bool(out.read_text()))
    deadline = float(out.read_text())
    assert 10 < deadline - time.monotonic() <= 12
    assert _alive(workers)
    await workers.stop_all()
    assert workers.status()[0]["last_exit"] is not None
    assert not _alive(workers)


def test_the_deadline_stops_moving_once_the_lease_is_not_fresh() -> None:
    lease = _Lease(remaining=12.0)
    workers = child.ChildWorkers(lease)
    workers._send_deadline()
    sent = workers._deadline
    lease.remaining = 0.0  # lost
    time.sleep(0.01)
    workers._send_deadline()
    assert workers._deadline == sent


@pytest.mark.asyncio
async def test_a_child_that_ignores_sigterm_is_killed_at_the_deadline_plus_the_drain(
    monkeypatch, tmp_path
) -> None:
    ready = _stubborn(monkeypatch, tmp_path)
    lease = _Lease(remaining=0.3)
    workers = child.ChildWorkers(lease)
    workers.start_all()
    await _until(ready.exists)
    lease.remaining = 0.0  # the lease is lost: the deadline stays where it was
    started = time.monotonic()
    await workers.stop_all(drain_timeout_s=0.3)
    assert time.monotonic() - started < 2.0  # not STOP_TIMEOUT_S
    assert workers.status()[0]["last_exit"] == -signal.SIGKILL


@pytest.mark.asyncio
async def test_a_cancelled_stop_kills_the_child(monkeypatch, tmp_path) -> None:
    ready = _stubborn(monkeypatch, tmp_path)
    workers = child.ChildWorkers(_Lease(remaining=30.0))
    workers.start_all()
    await _until(ready.exists)
    proc = workers._proc
    assert proc is not None
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(workers.stop_all(), timeout=0.3)
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_a_child_that_exits_unasked_is_started_again(monkeypatch) -> None:
    monkeypatch.setattr(child, "RESTART_DELAY_S", 0.01)
    monkeypatch.setattr(child, "child_argv", lambda fd, _alive_fd: [sys.executable, "-c", "pass"])
    workers = child.ChildWorkers(_Lease())
    workers.start_all()
    await _until(lambda: workers.restarts >= 2)
    await workers.stop_all()


@pytest.mark.asyncio
async def test_a_child_that_keeps_failing_to_start_gives_the_lease_up(monkeypatch) -> None:
    monkeypatch.setattr(child, "RESTART_DELAY_S", 0.01)
    monkeypatch.setattr(child, "MAX_FAILED_STARTS", 3)
    monkeypatch.setattr(
        child, "child_argv", lambda fd, _alive_fd: [sys.executable, "-c", "exit(3)"]
    )
    lease = _Lease()
    workers = child.ChildWorkers(lease)
    workers.start_all()
    await _until(lambda: bool(lease.given_up))
    assert workers.failed_starts == 3 and workers.restarts == 2
    assert "failed 3 starts" in lease.given_up[0] and "code 3" in lease.given_up[0]
    assert workers._watch is not None and workers._watch.done()


@pytest.mark.asyncio
async def test_a_child_that_cannot_be_created_gives_the_lease_up_and_leaks_no_fd(
    monkeypatch, tmp_path
) -> None:
    """An OSError before any child exists (exec, fork, a pipe) is a failed start
    like an early exit, and every descriptor it opened is closed."""
    monkeypatch.setattr(child, "RESTART_DELAY_S", 0.01)
    monkeypatch.setattr(child, "MAX_FAILED_STARTS", 3)
    missing = str(tmp_path / "no-such-python")
    monkeypatch.setattr(child, "child_argv", lambda fd, _alive_fd: [missing])
    fds = _open_fds()
    lease = _Lease()
    workers = child.ChildWorkers(lease)
    workers.start_all()
    await _until(lambda: bool(lease.given_up))
    assert workers.failed_starts == 3 and "could not start" in lease.given_up[0]
    assert workers._watch is not None and workers._watch.done()
    assert workers._write_fd is None and workers._alive_fd is None
    if fds is not None:
        assert _open_fds() <= fds


# A stand-in child whose event loop beats, then hangs.
_HANGS = """
import asyncio, sys, time
from server.workers import child
async def main():
    task = asyncio.create_task(child.beat(int(sys.argv[1])))
    await asyncio.sleep(0.5)
    time.sleep(60)  # the loop hangs
asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_a_child_whose_loop_hangs_is_not_alive(monkeypatch) -> None:
    monkeypatch.setattr(child, "ALIVE_WINDOW_S", 1.5)
    monkeypatch.setattr(child, "PUBLISH_PERIOD_S", 0.1)
    monkeypatch.setattr(
        child, "child_argv", lambda _fd, alive_fd: [sys.executable, "-c", _HANGS, str(alive_fd)]
    )
    workers = child.ChildWorkers(_Lease())
    workers.start_all()
    await _until(lambda: workers._proc is not None)
    started = workers._beat_at
    for _ in range(600):  # it beats, once its imports are done
        if workers._beat_at > started:
            break
        await asyncio.sleep(0.05)
    assert workers._beat_at > started and _alive(workers)
    await asyncio.sleep(2.5)
    assert workers._proc.returncode is None and not _alive(workers)
    await workers.stop_all(drain_timeout_s=0.1)


def test_child_lease_follows_the_pipe_and_goes_stale_on_eof() -> None:
    lease = child.ChildLease()
    assert not lease.fresh() and not lease.held
    r, w = os.pipe()
    stopped = threading.Event()
    t = threading.Thread(target=child.read_deadlines, args=(r, lease, stopped.set))
    t.start()
    os.write(w, f"{time.monotonic() + 30!r}\n".encode())
    for _ in range(100):
        if lease.fresh():
            break
        time.sleep(0.01)
    assert lease.fresh()
    os.close(w)  # the parent died
    t.join(timeout=2)
    assert stopped.is_set() and not lease.fresh()


def test_the_child_exits_by_itself_past_the_deadline() -> None:
    """A parent stopped (SIGSTOP) sends no new deadline: the child ends
    `grace_s` after the last one, whatever its shutdown is doing."""
    code = (
        "import threading, time\n"
        "from server.workers import child\n"
        "lease = child.ChildLease()\n"
        "lease.deadline = time.monotonic() + 0.2\n"
        "threading.Thread(target=child.exit_past_deadline, args=(lease, 0.2)).start()\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    rc = subprocess.run([sys.executable, "-c", code], timeout=20).returncode
    assert rc == 1 and time.monotonic() - started < 15


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux")
def test_the_child_dies_with_its_parent(tmp_path) -> None:
    pid_file = tmp_path / "pid"
    grandchild = (
        "from server.workers import child; import time; child.die_with_parent(); time.sleep(60)"
    )
    parent = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", parent])
    try:
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text():
                break
            time.sleep(0.05)
        pid = int(pid_file.read_text())
        time.sleep(1.0)  # the grandchild has set its death signal
        proc.kill()
        proc.wait()
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        pytest.fail("the child outlived its parent")
    finally:
        if proc.poll() is None:
            proc.kill()


def test_child_env_runs_the_workers_without_a_lease() -> None:
    env = child.child_env({"LOOM_BACKGROUND_WORKERS": "auto", "PATH": "/bin"})
    assert env["LOOM_BACKGROUND_WORKERS"] == "on"
    assert env["LOOM_WORKERS_PROCESS"] == "inline"
    assert env["PATH"] == "/bin"


def test_an_auto_holder_runs_its_workers_in_a_child(monkeypatch) -> None:
    from server.settings import settings
    from server.workers.leader import SupervisorLease
    from server.workers.supervisor import WorkerSupervisor

    lease = SupervisorLease("auto")
    monkeypatch.setattr(settings, "workers_process", "child")
    assert isinstance(child.worker_runner(lease), child.ChildWorkers)
    assert child.current_runner() is child._RUNNER
    monkeypatch.setattr(settings, "workers_process", "inline")
    assert isinstance(child.worker_runner(lease), WorkerSupervisor)
    monkeypatch.setattr(settings, "workers_process", "child")
    assert isinstance(child.worker_runner(SupervisorLease("on")), WorkerSupervisor)


def test_the_child_runs_its_own_entry_point_not_the_serving_app() -> None:
    assert child.child_argv(5, 6)[1:] == [
        "-m",
        "server.worker_main",
        "--lease-fd",
        "5",
        "--alive-fd",
        "6",
    ]
    code = (
        "import sys, server.worker_main\n"
        "assert 'server.main' not in sys.modules, 'the serving app was imported'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=120)


@pytest.mark.asyncio
async def test_the_child_stops_its_loops_before_its_writes_drain(monkeypatch) -> None:
    from server import worker_main
    from server.core import ch as ch_mod
    from server.insights.app import provider_barrier
    from server.memory.app import embedder_calibration

    events: list[str] = []

    class _Ch:
        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

    class _Sup:
        def start_all(self) -> None:
            return None

        async def stop_all(self, drain_timeout_s: float) -> None:
            events.append("workers stopped")

    async def _service() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.05)  # a loop that takes a moment to finish
            events.append("service ended")

    async def _start_services(_ch) -> list[asyncio.Task[None]]:
        return [asyncio.create_task(_service())]

    async def _drain() -> None:
        events.append("drain")

    async def _retire() -> None:
        return None

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Ch())
    monkeypatch.setattr(worker_main, "_start_services", _start_services)
    monkeypatch.setattr(worker_main, "get_supervisor", lambda: _Sup())
    monkeypatch.setattr(worker_main, "_drain", _drain)
    monkeypatch.setattr(provider_barrier, "retire_process", _retire)
    monkeypatch.setattr(
        embedder_calibration, "stop_refresh_loop", lambda: events.append("calibration stopped")
    )
    monkeypatch.setattr(child, "exit_past_deadline", lambda lease, grace_s: None)
    monkeypatch.setattr(child.leader, "_CURRENT", None)  # _serve sets the child's lease
    lease_r, lease_w = os.pipe()
    alive_r, alive_w = os.pipe()
    serve = asyncio.create_task(worker_main._serve(lease_r, alive_w))
    await asyncio.sleep(0.1)
    os.close(lease_w)  # the parent went: the child stops
    await asyncio.wait_for(serve, timeout=10)
    os.close(alive_r)
    assert events == ["calibration stopped", "service ended", "workers stopped", "drain"]


@pytest.fixture
def child_stubs(monkeypatch) -> SimpleNamespace:
    """The child's services stubbed to start without ClickHouse. `tags` holds the
    query tag each tracker-registry refresh saw; `second_refresh` is set on the second."""
    from server import boot
    from server.code.app.index import repo_registry
    from server.core import runtime_overrides
    from server.core.ch import current_query_tag
    from server.insights.app import provider_barrier, provider_runtime
    from server.tracker.app import registry_sources

    tags: list[str] = []
    second_refresh = asyncio.Event()

    async def _no_overrides(*, ch=None) -> runtime_overrides.Snapshot:
        return {}

    async def _noop(*_a, **_k) -> None:
        return None

    async def _refresh(*_a, **_k) -> None:
        tags.append(current_query_tag())
        if len(tags) == 2:
            second_refresh.set()

    async def _refresher(_ch):
        return asyncio.create_task(asyncio.sleep(3600))

    monkeypatch.setattr(provider_barrier, "register_process", _noop)
    monkeypatch.setattr(provider_runtime, "load_active_configuration", _noop)
    monkeypatch.setattr(provider_runtime, "refresh_loop", lambda: asyncio.sleep(3600))
    monkeypatch.setattr(registry_sources, "refresh_db_sources", _refresh)
    monkeypatch.setattr(repo_registry, "start_refresher", _refresher)
    monkeypatch.setattr(boot, "calibrate_similarity_gates", _noop)
    monkeypatch.setattr("server.observability.get_emitter", lambda: SimpleNamespace(start=_noop))
    monkeypatch.setattr(runtime_overrides, "load", _no_overrides)
    monkeypatch.setattr(boot, "_WARM", {})  # no earlier test's first load counts
    return SimpleNamespace(tags=tags, second_refresh=second_refresh)


async def _stop(tasks: list[asyncio.Task[None]]) -> None:
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("child_stubs")
async def test_the_child_adopts_the_runtime_overrides_before_its_workers_start(
    monkeypatch,
) -> None:
    """A worker pass reads LIVE settings: the child loads the stored overrides
    before the supervisor starts and keeps re-reading them."""
    from server import worker_main
    from server.core import runtime_overrides, settings_overrides
    from server.settings import settings

    async def _load(*, ch=None) -> runtime_overrides.Snapshot:
        await asyncio.sleep(0.05)  # the first read is not instant
        return {runtime_overrides.SETTING: {("memory_ask_recency_weight",): 0.9}}

    monkeypatch.setattr(runtime_overrides, "load", _load)
    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.2)
    tasks = await worker_main._start_services(object())
    try:
        assert settings.memory_ask_recency_weight == 0.9  # before any pass starts
    finally:
        await _stop(tasks)
        settings_overrides.adopt({})


@pytest.mark.asyncio
async def test_the_child_tags_its_boot_reads_and_its_tracker_loop(monkeypatch, child_stubs) -> None:
    """Its boot reads and its tracker-registry loop run where no request or pass
    tag reaches; each carries its own, and the caller's context keeps none."""
    from server import worker_main
    from server.core.ch import current_query_tag

    real_sleep = asyncio.sleep
    # The loop waits at least 5 s between refreshes: let every wait return at once.
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    tasks = await worker_main._start_services(object())
    try:
        await asyncio.wait_for(child_stubs.second_refresh.wait(), 5)
    finally:
        await _stop(tasks)
    assert child_stubs.tags[:2] == ["boot.workers_child", "tracker.sources_refresh"]
    assert current_query_tag() == ""
