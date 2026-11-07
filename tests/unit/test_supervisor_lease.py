"""One supervisor per deployment: the ClickHouse lease every Loom process
contends for, and the rate limiter's awareness that N workers are N replicas."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from server.core.schema.migrate_lease import KeeperLock, LeaseConfig
from server.workers import leader
from server.workers.leader import SupervisorLease, keep_lease
from tests.unit.test_migrate_keeper_lock import Clock, FakeKeeper


class _Lock:
    """KeeperLock's shape: acquire / renew_if_due / release / held / holder /
    kind / cfg / clock / last_renew. Renewals land unless `lost`."""

    kind = "keeper"
    error = ""

    def __init__(self, free: list[bool], cfg: LeaseConfig | None = None) -> None:
        self.free = free  # shared between contenders: [True] while nobody holds it
        self.held = False
        self.holder = f"h{id(self)}"
        self.lost = False
        self.cfg = cfg or leader.lease_config(30)
        self.clock = time.monotonic
        self.last_renew = 0.0
        self.ch = type("C", (), {"close": lambda self: None})()

    def acquire(self) -> str:
        if self.free[0]:
            self.free[0], self.held = False, True
            self.last_renew = self.clock()
        return "held" if self.held else "timeout"

    def renew_if_due(self) -> None:
        if self.lost:
            self.held = False
        else:
            self.last_renew = self.clock()

    def release(self) -> None:
        if self.held:
            self.free[0], self.held = True, False


def _auto(lock: _Lock) -> SupervisorLease:
    lease = SupervisorLease("auto")
    lease._lock = lock
    return lease


def test_of_two_contenders_one_holds_and_the_other_takes_over_on_release() -> None:
    free = [True]
    a, b = _auto(_Lock(free)), _auto(_Lock(free))
    a.step()
    b.step()
    assert a.held and not b.held
    assert "holds workers_supervisor" in a.reason and "held by another" in b.reason
    a.release()
    b.step()
    assert b.held and not a.held


def test_modes_on_and_off_take_no_lease() -> None:
    on, off = SupervisorLease("on"), SupervisorLease("off")
    on.step()
    off.step()
    assert on.held and not off.held
    assert asyncio.run(leader.decide_supervisor("off")).held is False


async def test_keep_lease_starts_on_acquire_and_stops_on_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "supervisor_lease_ttl_s", 24)
    monkeypatch.setattr(leader.asyncio, "sleep", _fast_sleep)
    lock = _Lock([False])
    lease = _auto(lock)
    events: list[str] = []

    async def lost() -> None:
        events.append("stop")

    task = asyncio.create_task(keep_lease(lease, lambda: events.append("start"), lost))
    await _until(lambda: events == [])
    lock.free[0] = True  # the holder elsewhere exited
    await _until(lambda: events == ["start"])
    lock.lost = True  # a renewal found another holder
    await _until(lambda: events == ["start", "stop"])
    task.cancel()
    assert not lease.held


_real_sleep = asyncio.sleep


async def _fast_sleep(_s: float) -> None:
    await _real_sleep(0.001)


async def _until(cond) -> None:
    for _ in range(400):
        if cond():
            return
        await _real_sleep(0.005)
    assert cond()


def test_settings_defaults() -> None:
    from server.settings import Settings

    s = Settings()
    assert s.web_workers == 1
    assert s.background_workers == "auto"
    assert s.supervisor_lease_ttl_s == 30


def test_rate_limiter_counts_web_workers_as_replicas(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import ratelimit
    from server.settings import settings

    monkeypatch.setattr(settings, "replica_count", 2)
    monkeypatch.setattr(settings, "web_workers", 4)
    assert ratelimit.effective_replicas() == 8
    monkeypatch.setattr(settings, "web_workers", 0)
    assert ratelimit.effective_replicas() == 2  # 0/unset = one process


async def test_a_supervisor_stopped_by_a_lost_lease_runs_again_when_regained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers import status_store
    from server.workers import supervisor as sup
    from server.workers.registry import WorkerSpec

    async def _noop() -> None:
        return None

    async def _drained() -> None:
        return None

    monkeypatch.setattr(sup, "enabled_workers", lambda: [WorkerSpec("w", 3600, _noop)])
    monkeypatch.setattr(status_store, "drain", _drained)
    s = sup.WorkerSupervisor()
    s.start_all()
    await s.stop_all(drain_timeout_s=1)
    s.start_all()
    await _real_sleep(0.05)
    assert s._tasks and not any(t.done() for t in s._tasks)  # the worker loop did not exit
    await s.stop_all(drain_timeout_s=1)


def test_the_lock_follows_the_server_not_a_failed_create(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blip or a Keeper error raises (the next round retries); a server with no
    Keeper configured raises KeeperRequired."""
    from types import SimpleNamespace

    from server.core.schema import migrate

    class _CH:
        def __init__(self, engine: str, error: Exception | None = None) -> None:
            self.engine, self.error, self.closed = engine, error, False

        def query(self, sql: str, **_: Any) -> Any:
            return SimpleNamespace(result_rows=[(self.engine,)] if self.engine else [])

        def command(self, sql: str) -> None:
            if self.error:
                raise self.error

        def close(self) -> None:
            self.closed = True

    for error in (
        ConnectionError("Code: 209. Timeout exceeded"),
        RuntimeError("Code: 999. DB::Exception: Coordination error (KEEPER_EXCEPTION)"),
    ):
        ch = _CH("", error)
        monkeypatch.setattr(migrate, "client", lambda _ch=ch, **_: _ch)
        with pytest.raises(type(error)):
            leader._lock("workers_supervisor")
        assert ch.closed
    for engine, error in (("KeeperMap", RuntimeError("Code: 999. never asked")), ("", None)):
        ch = _CH(engine, error)
        monkeypatch.setattr(migrate, "client", lambda _ch=ch, **_: _ch)
        lock = leader._lock("workers_supervisor")
        assert lock.kind == "keeper" and lock.label == "supervisor"
    from server.core.schema.migrate_lease import KeeperRequired

    ch = _CH("", RuntimeError("Code: 139. DB::Exception: No keeper_map_path_prefix"))
    monkeypatch.setattr(migrate, "client", lambda _ch=ch, **_: _ch)
    with pytest.raises(KeeperRequired):
        leader._lock("workers_supervisor")
    assert ch.closed


def test_the_drain_after_a_loss_fits_in_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "supervisor_lease_ttl_s", 24)
    assert leader.drain_timeout_s() == 4.0
    monkeypatch.setattr(settings, "supervisor_lease_ttl_s", 120)
    assert leader.drain_timeout_s() == 10.0


async def test_a_worker_that_did_not_drain_is_not_started_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers import status_store
    from server.workers import supervisor as sup
    from server.workers.registry import WorkerSpec

    async def _noop() -> None:
        return None

    async def _drained() -> None:
        return None

    monkeypatch.setattr(sup, "enabled_workers", lambda: [WorkerSpec("w", 3600, _noop)])
    monkeypatch.setattr(status_store, "drain", _drained)
    s = sup.WorkerSupervisor()
    stuck = asyncio.Event()

    async def _stuck() -> None:
        await stuck.wait()

    s._tasks = [asyncio.create_task(_stuck(), name="loom-worker:w")]
    s.start_all()
    assert len(s._tasks) == 1  # the still-running one, not a second copy
    old = s._tasks[0]
    stuck.set()
    await old
    await _real_sleep(0)
    # ...and once it ends, the worker starts again: the supervisor is running.
    live = [t for t in s._tasks if not t.done()]
    assert len(live) == 1 and live[0] is not old and live[0].get_name() == "loom-worker:w"
    await s.stop_all(drain_timeout_s=1)
    # A worker that ends after the supervisor stopped is not restarted.
    s._tasks = [asyncio.create_task(_stuck(), name="loom-worker:w")]
    stuck.clear()
    s.start_all()
    s._stopping.set()
    stuck.set()
    await _real_sleep(0.01)
    assert not [t for t in s._tasks if not t.done()]


# -- the timings, with the real LeaseConfig ------------------------------------------


@pytest.mark.parametrize("ttl", [24, 30, 60, 120, 600])
def test_the_lease_timings_fit_inside_the_ttl(monkeypatch: pytest.MonkeyPatch, ttl: int) -> None:
    """renew T/6, retried every T/12, lose T/2, drain T/6, expiry T: the
    workers are stopped before anyone can take over, and a renewal that hangs
    for its whole timeout still leaves a retry before the holder gives up."""
    from server.settings import settings

    monkeypatch.setattr(settings, "supervisor_lease_ttl_s", ttl)
    cfg = leader.lease_config(ttl)
    held_step = leader._period_s(held=True)
    timeout = leader.call_timeout_s(ttl)
    assert cfg.renew_after == ttl / 6 and cfg.lose_after == ttl / 2
    assert held_step < cfg.renew_after
    # A renewal is two statements: due, three hung calls, the next step, two
    # more: still inside lose_after.
    assert cfg.renew_after + 3 * timeout + held_step + 2 * timeout <= cfg.lose_after
    assert cfg.lose_after + leader.drain_timeout_s() <= ttl * 2 / 3 < ttl


def test_the_minimum_ttl_is_the_one_the_timings_hold_for() -> None:
    from pydantic import ValidationError

    from server.settings import Settings

    with pytest.raises(ValidationError):
        Settings(supervisor_lease_ttl_s=23)
    assert Settings(supervisor_lease_ttl_s=24).supervisor_lease_ttl_s == 24


def _keeper_lease(clock: Clock, ttl: int = 30) -> tuple[SupervisorLease, FakeKeeper]:
    ch = FakeKeeper(clock, name=leader.LEASE_NAME)
    lease = SupervisorLease("auto")
    lease._lock = KeeperLock(
        ch,
        cfg=leader.lease_config(ttl),
        lock_name=leader.LEASE_NAME,
        label="supervisor",
        sleep=clock.sleep,
        clock=clock.monotonic,
        out=lambda _line: None,
    )
    return lease, ch


def _down(ch: FakeKeeper) -> Any:
    real = ch.command
    ch.command = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))  # type: ignore[method-assign]
    return real


def test_one_failed_renewal_keeps_the_lease_and_the_workers() -> None:
    """The holder steps every T/12 and renews at T/6, so a renewal that
    fails is retried long before T/2. One failure must not stop the workers."""
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    lease.step()
    assert lease.held and lease.fresh()
    step = 30 / 12
    real = _down(ch)
    for _ in range(2):  # the renewal due at T/6 fails
        clock.sleep(step)
        lease.step()
    assert lease.held and lease.fresh()
    ch.command = real  # type: ignore[method-assign]
    clock.sleep(step)
    lease.step()  # the retry lands
    assert lease.held and lease.remaining_s() == pytest.approx(15.0)


def test_a_holder_that_gave_up_takes_its_own_key_back_on_the_next_round() -> None:
    """After T/2 of failed renewals the holder stops, but the Keeper key
    still names it: the next round renews it back instead of waiting until it
    is stale (no supervisor anywhere for the rest of the TTL)."""
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    lease.step()
    real = _down(ch)
    while lease.held:
        clock.sleep(30 / 12)
        lease.step()
    assert clock.t < 30 and not lease.fresh()
    ch.command = real  # type: ignore[method-assign]
    clock.sleep(30 / 6)
    lease.step()
    assert lease.held and lease.fresh() and clock.t < 30
    assert "holds workers_supervisor" in lease.reason


async def test_the_workers_stop_at_the_deadline_while_a_renewal_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal call that hangs does not keep the workers running past
    lose_after; the loop's deadline stops them, and they start again once a
    renewal lands."""
    monkeypatch.setattr(leader.asyncio, "sleep", _fast_sleep)
    lock = _Lock([True], cfg=LeaseConfig(ttl_s=30, renew_after_s=0.05, lose_after_s=0.3))
    lease = _auto(lock)
    lease.step()
    gate = threading.Event()
    real_renew = lock.renew_if_due

    def hang() -> None:
        gate.wait(5)
        real_renew()

    lock.renew_if_due = hang  # type: ignore[method-assign]
    events: list[str] = []

    async def lost() -> None:
        events.append("stop")

    task = asyncio.create_task(keep_lease(lease, lambda: events.append("start"), lost))
    started = time.monotonic()
    await _until(lambda: events == ["stop"])
    assert time.monotonic() - started < 1.0  # at the deadline, not after the hung call
    assert lease.held and not lease.fresh()
    gate.set()  # the call returns and lands
    await _until(lambda: events == ["stop", "start"])
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_failing_start_or_stop_does_not_end_the_lease_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leader.asyncio, "sleep", _fast_sleep)
    lock = _Lock([True])
    lease = _auto(lock)
    events: list[str] = []

    def start() -> None:
        events.append("start")
        if events.count("start") == 1:
            raise RuntimeError("enabled_workers failed")

    async def lost() -> None:
        events.append("stop")
        raise RuntimeError("stop_all failed")

    task = asyncio.create_task(keep_lease(lease, start, lost))
    # The first start fails: whatever started is stopped, and the next round retries.
    await _until(lambda: events[:3] == ["start", "stop", "start"])
    lock.lost = True
    await _until(lambda: events[-1] == "stop" and len(events) == 4)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_holder_whose_lease_is_stale_starts_no_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process resuming from a pause (SIGSTOP past the TTL) must not run
    a pass before its lease loop has looked: the pass checks the last renewal
    on the monotonic clock, with no ClickHouse call."""
    from server.workers import status_store
    from server.workers import supervisor as sup
    from server.workers.registry import WorkerSpec

    passes: list[int] = []

    async def _pass() -> None:
        passes.append(1)

    async def _drained() -> None:
        return None

    lock = _Lock([True], cfg=LeaseConfig(ttl_s=30, lose_after_s=15))
    lease = _auto(lock)
    lease.step()
    lock.last_renew = time.monotonic() - 60  # paused for a minute
    monkeypatch.setattr(leader, "_CURRENT", lease)
    monkeypatch.setattr(sup, "_STALE_LEASE_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(status_store, "drain", _drained)
    monkeypatch.setattr(sup, "_begin_loop", _no_stagger)
    s = sup.WorkerSupervisor()
    monkeypatch.setattr(sup, "enabled_workers", lambda: [WorkerSpec("w", 3600, _pass)])
    s.start_all()
    await _real_sleep(0.1)
    assert passes == []
    lock.last_renew = time.monotonic()  # renewed
    await _until(lambda: passes == [1])
    await s.stop_all(drain_timeout_s=1)


async def _no_stagger(_spec: Any) -> Any:
    from types import SimpleNamespace

    return 0.0, SimpleNamespace(cursor=None)


def test_modes_other_than_auto_are_always_fresh() -> None:
    assert SupervisorLease("on").fresh()
    assert SupervisorLease("auto").fresh() is False  # nothing acquired yet


async def test_a_process_that_becomes_the_leader_applies_the_retention_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the boot leader applied LOOM_RETENTION_*_DAYS; a process that takes
    the lease later does its boot work too."""
    from server import boot
    from server.settings import settings
    from server.storage import retention_overrides

    monkeypatch.setattr(settings, "background_workers", "auto")
    monkeypatch.setattr(settings, "deployment", True)
    captured: dict[str, Any] = {}

    async def _keep(lease: Any, on_acquired: Any, on_lost: Any) -> None:
        captured["on_acquired"] = on_acquired

    applied: list[bool] = []

    async def _apply(_settings: Any, ch: Any = None, *, leader: bool, **_: Any) -> int:
        applied.append(leader)
        return 0

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(leader, "keep_lease", _keep)
    monkeypatch.setattr(retention_overrides, "apply", _apply)
    monkeypatch.setattr(boot, "register_tracker_namespaces", _noop)
    monkeypatch.setattr(boot, "publish_deployment_config", lambda ch: None)
    started: list[str] = []
    sup = type("S", (), {"start_all": lambda self: started.append("start")})()
    task = boot.start_lease_loop(SupervisorLease("auto"), sup, object())
    assert task is not None
    await task
    captured["on_acquired"]()
    await _until(lambda: applied == [True])
    assert started == ["start"]


async def test_shutdown_stops_the_supervisor_whatever_the_lease_says() -> None:
    """Workers that did not drain after a lost lease still run at shutdown:
    they are stopped first, and only then is the lease given up."""
    calls: list[str] = []

    class _Sup:
        async def stop_all(self, drain_timeout_s: float) -> None:
            calls.append("stop")

    lease = _auto(_Lock([True]))
    lease.held = False  # the lease was lost; a worker may still run
    lease.release = lambda: calls.append("release")  # type: ignore[method-assign]
    await leader.shutdown(lease, _Sup())
    assert calls == ["stop", "release"]


async def test_shutdown_gives_the_lease_up_when_the_budget_cuts_the_stop_short() -> None:
    released: list[bool] = []

    class _Slow:
        async def stop_all(self, drain_timeout_s: float) -> None:
            await asyncio.sleep(60)

    lease = _auto(_Lock([True]))
    lease.release = lambda: released.append(True)  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(leader.shutdown(lease, _Slow()), timeout=0.05)
    assert released == [True]


def test_a_holder_that_counted_its_key_lost_deletes_it_on_shutdown() -> None:
    """SIGTERM after missed renewals: the key still names this holder, so it
    is deleted and the successor need not wait out the TTL."""
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    ch.close = lambda: None  # type: ignore[attr-defined]
    lease.step()
    real = _down(ch)
    while lease._lock.held:
        clock.sleep(30 / 12)
        lease.step()
    ch.command = real  # type: ignore[method-assign]
    assert ch.row is not None and ch.row["holder"] == lease._lock.holder
    lease.release()
    assert ch.row is None
    # A key someone else took over is left alone.
    other, ch2 = _keeper_lease(clock)
    ch2.close = lambda: None  # type: ignore[attr-defined]
    other.step()
    ch2.row["holder"] = "someone-else"
    other.release()
    assert ch2.row is not None and ch2.row["holder"] == "someone-else"


@pytest.mark.parametrize("ttl", [24, 30, 47, 48, 60, 120, 600])
def test_a_renewal_that_hangs_on_both_statements_still_leaves_a_retry(
    monkeypatch: pytest.MonkeyPatch, ttl: int
) -> None:
    """A renewal is two statements (the UPDATE and the read-back), each with
    the call timeout. One hung on each still leaves a retry that lands in time."""
    clock = Clock()
    lease, ch = _keeper_lease(clock, ttl=ttl)
    lease.step()
    cfg, timeout = leader.lease_config(ttl), leader.call_timeout_s(ttl)
    acquired_at = lease._lock.last_renew
    statements: list[str] = []
    real_command, real_query = ch.command, ch.query

    def hung(*_a: Any, **_k: Any) -> Any:
        statements.append("hung")
        clock.sleep(timeout)
        raise TimeoutError("read timeout")

    ch.command = hung  # type: ignore[method-assign]
    clock.sleep(cfg.renew_after)
    lease.step()  # due: the UPDATE hangs for its timeout
    ch.command, ch.query = real_command, hung  # type: ignore[method-assign]
    clock.sleep(ttl / 12)
    lease.step()  # the retry: the UPDATE lands, the read-back hangs
    assert statements == ["hung", "hung"]
    ch.command, ch.query = real_command, real_query  # type: ignore[method-assign]
    clock.sleep(ttl / 12)
    lease.step()  # the next retry lands
    assert clock.t - acquired_at <= cfg.lose_after
    assert lease.held and lease.fresh()


def test_the_lease_client_keeps_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A renewal that timed out here but still runs on the server would
    lock a session, and the next call would fail with code 373."""
    import clickhouse_connect

    from server.core.schema import migrate

    real_client = migrate.client
    seen: dict[str, Any] = {}
    monkeypatch.setattr(migrate, "client", lambda **kw: seen.update(kw) or _NoKeeperCH())
    leader._lock("workers_supervisor")
    assert seen["session"] is False and seen["retries"] is False

    captured: dict[str, Any] = {}

    class _Fake:
        pass

    monkeypatch.setattr(
        clickhouse_connect, "get_client", lambda **kw: captured.update(kw) or _Fake()
    )
    real_client(receive_timeout_s=2, retries=False, session=False)
    assert captured["autogenerate_session_id"] is False
    real_client()
    assert captured["autogenerate_session_id"] is True


class _NoKeeperCH:
    def query(self, sql: str, **_: Any) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(result_rows=[("KeeperMap",)])

    def close(self) -> None:
        pass


def test_an_unreachable_lease_is_a_health_problem_not_another_holder() -> None:
    from server.observability.problems import HealthState, collect_problems
    from server.settings import Settings

    clock = Clock()
    lease, ch = _keeper_lease(clock)
    ch.query = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("Code: 497. DB::Exception: not enough privileges. (ACCESS_DENIED)")
    )
    lease.step()
    # `/v1/health` (workers_leader, the lease problem) shows it without a token: the kind.
    assert not lease.held and lease.error == "RuntimeError"
    assert "privileges" not in lease.reason and "RuntimeError" in lease.reason
    assert "held by another process" not in lease.reason
    codes = {
        p.code: p
        for p in collect_problems(
            Settings(background_workers="auto"),
            HealthState(worker_lease_held=False, worker_lease_error=lease.error, env={}),
        )
    }
    assert "workers_lease_unreachable" in codes and "workers_lease_absent" not in codes
    assert codes["workers_lease_unreachable"].severity == "warn"
    # Unreachable at all (no client): the same.
    broken = SupervisorLease("auto")

    def _no_client(_name: str) -> Any:
        raise ConnectionError("connection refused")

    import server.workers.leader as mod

    real = mod._lock
    mod._lock = _no_client
    try:
        with pytest.raises(ConnectionError):
            broken.step()
    finally:
        mod._lock = real
    assert "unreachable" in broken.reason and broken.error == "ConnectionError"
    assert "connection refused" not in broken.reason


def test_a_waiting_contender_sends_no_insert_while_the_key_is_held() -> None:
    """Each failed INSERT is a KEEPER_EXCEPTION in the server's
    log; a contender reads the key first."""
    clock = Clock()
    holder, ch = _keeper_lease(clock)
    holder.step()
    waiting = SupervisorLease("auto")
    waiting._lock = KeeperLock(
        ch,
        cfg=leader.lease_config(30),
        lock_name=leader.LEASE_NAME,
        label="supervisor",
        sleep=clock.sleep,
        clock=clock.monotonic,
        out=lambda _line: None,
    )
    before = len(ch.commands)
    for _ in range(5):
        waiting.step()
        clock.sleep(5)
        holder.step()
    assert not waiting.held
    assert not [sql for sql, _p in ch.commands[before:] if sql.startswith("INSERT")]


# -- whose lease: the deployment's, or a process's own ------------------------------


def test_auto_runs_no_workers_outside_the_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "background_workers", "auto")
    monkeypatch.setattr(settings, "deployment", False)
    assert settings.workers_mode() == "off"
    lease = asyncio.run(leader.decide_supervisor(settings.workers_mode()))
    assert not lease.held and "outside the deployment" in lease.reason
    assert lease.advertised() == ""
    monkeypatch.setattr(settings, "background_workers", "on")
    assert settings.workers_mode() == "on"  # the explicit opt-in
    monkeypatch.setattr(settings, "background_workers", "auto")
    monkeypatch.setattr(settings, "deployment", True)
    assert settings.workers_mode() == "auto"


def test_only_the_deployments_holder_runs_the_leader_duties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An `on` process outside the deployment runs workers, but not the
    retention ALTERs, the tracker rows or the provider participant sweep."""
    from server.insights.app import provider_coordinator
    from server.settings import settings

    monkeypatch.setattr(leader, "_CURRENT", SupervisorLease("on"))
    monkeypatch.setattr(settings, "deployment", False)
    assert not leader.leads_deployment() and not provider_coordinator._is_leader()
    monkeypatch.setattr(settings, "deployment", True)
    assert leader.leads_deployment() and provider_coordinator._is_leader()
    monkeypatch.setattr(leader, "_CURRENT", SupervisorLease("off"))
    assert not leader.leads_deployment()


def test_the_instance_row_names_the_lease_only_while_this_process_contends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import readiness

    clock = Clock()
    lease, ch = _keeper_lease(clock)
    ch.close = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(leader, "_CURRENT", lease)
    assert leader.advertised_lease() == "workers_supervisor"
    lease.give_up("the workers child failed 5 starts in a row")
    assert leader.advertised_lease() == ""
    lease.given_up = ""
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: ["LOOM_EMBEDDING_DIM"])
    assert leader.advertised_lease() == ""
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: [])
    readiness.begin_drain()
    assert leader.advertised_lease() == ""
    monkeypatch.setattr(leader, "_CURRENT", SupervisorLease("on"))
    assert leader.advertised_lease() == ""


# -- a build older than another contender leaves the lease to it ---------------------


def _live(monkeypatch: pytest.MonkeyPatch, *rows: dict[str, Any]) -> None:
    from server.core import instances

    monkeypatch.setattr(instances, "live_rows", lambda: list(rows))


def _newer(name: str) -> dict[str, Any]:
    from server.core import instances

    return {"workers_lease": name, "schema_version": instances.schema_version() + 1}


def test_an_outdated_holder_stops_its_workers_then_hands_the_key_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    lease.step()
    assert lease.held
    _live(monkeypatch, _newer(lease.name))  # a newer build is up and contending
    clock.sleep(2.5)
    lease.step()  # this round: stop the workers, keep the key until they stopped
    assert not lease.held and not lease.fresh() and ch.row is not None
    assert "newer build" in lease.reason
    clock.sleep(2.5)
    lease.step()  # the workers stopped: the key goes
    assert ch.row is None
    lease.step()  # and the old build does not ask for it again
    assert ch.row is None and not lease.held


def test_an_outdated_build_contends_again_once_no_newer_build_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollback: the newer build is gone, so its rows stopped being live."""
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    _live(monkeypatch, _newer(lease.name))
    lease.step()
    assert not lease.held and ch.row is None
    _live(monkeypatch)
    lease.step()
    assert lease.held


def test_a_newer_build_of_another_lease_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch server's newer build contends for its own lease: the
    deployment's holder keeps the deployment's."""
    clock = Clock()
    lease, _ch = _keeper_lease(clock)
    _live(monkeypatch, _newer("workers_supervisor:iso:branch:18760"), {"workers_lease": ""})
    lease.step()
    clock.sleep(2.5)
    lease.step()
    assert lease.held


def test_a_given_up_lease_is_asked_for_again_after_the_cooldown() -> None:
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    ch.close = lambda: None  # type: ignore[attr-defined]
    lease.step()
    lease.give_up("child failed")
    assert ch.row is None and not lease.held and lease.given_up == "child failed"
    fresh, ch2 = _keeper_lease(clock)
    lease._lock = fresh._lock  # give_up closed the old client
    lease.step()
    assert ch2.row is None and not lease.held
    assert "given up: child failed" in lease.reason
    lease._given_up_at -= leader.GIVE_UP_COOLDOWN_S
    lease.step()
    assert lease.held and ch2.row is not None and lease.given_up == ""


def test_a_drifted_newer_build_does_not_take_the_lease_from_a_healthy_older_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import instances

    clock = Clock()
    older, ch = _keeper_lease(clock)
    older.step()
    assert older.held
    # The newer build's process, with an embedder the deployment does not run.
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: ["LOOM_EMBEDDING_DIM"])
    newer = _auto(_Lock([True]))
    newer.step()
    assert not newer.held and "configuration differs" in newer.reason
    row = {"workers_lease": newer.advertised(), "schema_version": instances.schema_version() + 1}
    # Back on the healthy older build: the newer row names no lease.
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: [])
    _live(monkeypatch, row)
    for _ in range(3):
        clock.sleep(2.5)
        older.step()
    assert older.held and ch.row is not None


def test_a_drifted_holder_stops_its_workers_then_gives_the_key_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    lease.step()
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: ["LOOM_JWT_SECRET"])
    clock.sleep(2.5)
    lease.step()  # stop the workers, keep the key until they stopped
    assert not lease.held and ch.row is not None
    assert "LOOM_JWT_SECRET" in lease.reason
    clock.sleep(2.5)
    lease.step()
    assert ch.row is None
    monkeypatch.setattr(leader.deployment_config, "drift", lambda: [])
    lease.step()  # fixed (a republished config): it contends again
    assert lease.held


async def test_a_draining_holder_gives_the_lease_up_whatever_runs_its_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SIGTERM marks the process draining: the lease loop stops the workers
    (inline or a child alike) and releases the key while the process still
    serves its ready delay."""
    from server.core import readiness

    monkeypatch.setattr(leader.asyncio, "sleep", _fast_sleep)
    clock = Clock()
    lease, ch = _keeper_lease(clock)
    lease.step()
    events: list[str] = []

    async def lost() -> None:
        events.append("stop")

    task = asyncio.create_task(keep_lease(lease, lambda: events.append("start"), lost))
    readiness.begin_drain()
    await _until(lambda: ch.row is None)
    assert events == ["stop"] and "draining" in lease.reason
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_a_draining_process_starts_no_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A step already running when SIGTERM came may still win the lease."""
    from server.core import readiness

    monkeypatch.setattr(leader.asyncio, "sleep", _fast_sleep)
    lease = _auto(_Lock([True]))
    events: list[str] = []
    readiness.begin_drain()
    monkeypatch.setattr(leader, "_stand_aside_reason", lambda: "")

    async def lost() -> None:
        events.append("stop")

    task = asyncio.create_task(keep_lease(lease, lambda: events.append("start"), lost))
    await _until(lambda: lease.held)
    await _real_sleep(0.05)
    assert events == []
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_the_lease_loop_stops_the_workers_of_a_given_up_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leader.asyncio, "sleep", _fast_sleep)
    lease = _auto(_Lock([True]))
    events: list[str] = []

    async def lost() -> None:
        events.append("stop")

    task = asyncio.create_task(keep_lease(lease, lambda: events.append("start"), lost))
    await _until(lambda: events == ["start"])
    await asyncio.to_thread(lease.give_up, "this process is draining")
    await _until(lambda: events == ["start", "stop"])
    assert "given up" in lease.reason  # not a renewal that went overdue
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_a_given_up_lease_is_a_health_problem() -> None:
    from server.observability.problems import HealthState, collect_problems
    from server.settings import Settings

    codes = {
        p.code
        for p in collect_problems(
            Settings(background_workers="auto"),
            HealthState(worker_lease_held=False, worker_lease_given_up="child failed", env={}),
        )
    }
    assert "workers_lease_given_up" in codes and "workers_lease_absent" not in codes
