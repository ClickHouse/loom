"""One workers supervisor per deployment, elected through ClickHouse.

Every process of the deployment (LOOM_DEPLOYMENT=1) on `auto` contends for one
lease, `workers_supervisor`: a key of the Keeper lock table
(`loom._migration_lock`, strict KeeperMap, so of two contenders one write
lands), its expiry judged on the ClickHouse server's clock. Outside the deployment (an
iso or branch server on the same ClickHouse) `auto` runs no workers, and only
the deployment's holder runs the leader's duties (`leads_deployment`).

The timings, all from the TTL T (`supervisor_lease_ttl_s`, `lease_config`):

  renew   every T/6; a failed renewal is retried every T/12
  lose    T/2 after the last renewal that landed was sent: the workers stop,
          even while a renewal call is still blocked (`keep_lease`'s deadline)
  drain   T/6: in-process workers get that long, and a workers child is
          killed then (`child.py`), so the workers are stopped by 2T/3
  expiry  T after the server stamped that renewal; only then can another
          process take over, so T/3 is the margin

A holder that counted the lease lost while its key still names it takes it
back at once. A holder that dies stops renewing and the next contender takes
the lease after T. There are no fencing tokens: a statement a stopped worker
already sent still lands; its pass stops at its next `pass_context.check_stop()`.

A process stands aside (a holder stops its workers, then gives the key up; a
contender does not ask) while it drains after SIGTERM, while its configuration
differs from the deployment's published one (`deployment_config.drift()`: it
is not ready, and could write vectors in the wrong space), and while a live
instance row (`server.core.instances`) contending for the same lease carries a
higher schema version (its build's last migration number). That last is the
rolling-upgrade hand-over: it fires only when the schema versions differ, so a
code-only release never hands over. Once no newer build is live (a rollback)
the older one contends again. The instance row names the lease only while
this process contends for it (`advertised_lease`), so a drifted, draining or
given-up process never makes an older build yield.

A process whose workers cannot start gives the lease up (`give_up`) and
contends again GIVE_UP_COOLDOWN_S later.

Modes (`LOOM_BACKGROUND_WORKERS`, as `settings.workers_mode()` resolves it):
  auto  — contend for the lease (default); `off` outside the deployment
  on    — always run (one process, or an operator who wants N copies); a cell's
          process runs only while its cell's gate lets it serve
  off   — never run (a pure web replica behind a load balancer)
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from server.core import cell, deployment_config, instances, readiness
from server.core.leases import has_live_holder
from server.core.schema.migrate_lease import (
    KeeperLock,
    LeaseConfig,
    cluster_lock,
    describe_error,
)
from server.settings import settings

log = logging.getLogger("loom.workers.leader")

LEASE_NAME = "workers_supervisor"
# A process that gave the lease up contends again this long later.
GIVE_UP_COOLDOWN_S = 300.0


class WorkersLease(Protocol):
    """What the supervisor, its passes and /v1/health read of this process's
    lease: a `SupervisorLease`, or in a workers child its parent's deadline."""

    held: bool
    reason: str
    error: str
    given_up: str

    def remaining_s(self) -> float: ...

    def fresh(self) -> bool: ...


def newer_build(name: str) -> int:
    """The schema version of a live newer build contending for `name`, else 0."""
    newest = max(
        (
            int(r.get("schema_version") or 0)
            for r in instances.live_rows()
            if r.get("workers_lease") == name
        ),
        default=0,
    )
    return newest if newest > instances.schema_version() else 0


def _mode_reason(mode: str) -> str:
    if mode == "off" and settings.auto_outside_deployment():
        return "LOOM_BACKGROUND_WORKERS=auto outside the deployment (no LOOM_DEPLOYMENT=1): off"
    return f"LOOM_BACKGROUND_WORKERS={mode}"


class SupervisorLease:
    """Whether this process runs the supervisor (`held`) and why (`reason`),
    for /v1/health. In `auto` mode it contends for the ClickHouse lease.
    `error`: why the last round could not ask ('' when it could).
    `given_up`: why this process stopped contending ('' while it contends)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.name = LEASE_NAME
        self._lock: KeeperLock | None = None
        # A step still running in its thread when shutdown releases.
        self._mutex = threading.Lock()
        # A cell's `on` process starts no worker until its own rows let it serve.
        self.held = mode == "on" and not settings.cell
        self.reason = _mode_reason(mode) if mode != "auto" else "not acquired yet"
        self.error = ""
        self.given_up = ""
        self._given_up_at = 0.0

    def step(self) -> None:
        """Acquire when free, renew when held. Blocking: run it in a thread."""
        if self.mode == "on" and settings.cell:
            # No lease to contend for, but a cell's gate still stops and starts them.
            aside = cell.gate_reason()
            self.held = not aside
            self.reason = (
                f"{_mode_reason(self.mode)}: {aside}" if aside else _mode_reason(self.mode)
            )
            return
        if self.mode != "auto":
            return
        with self._mutex:
            if self.given_up and time.monotonic() < self._given_up_at + GIVE_UP_COOLDOWN_S:
                return
            self.given_up = ""
            try:
                self._step()
            except Exception as e:
                self.error = describe_error(e)
                if not self.held:
                    self.reason = f"{self.name}: ClickHouse unreachable ({self.error})"
                raise

    def _step(self) -> None:
        if self._lock is None:
            self._lock = _lock(self.name)
        lock = self._lock
        if lock.held:
            lock.renew_if_due()
        aside = _stand_aside_reason()
        if not aside and (newer := newer_build(self.name)):
            aside = f"left to a newer build (schema {newer})"
        if aside and lock.held:
            self._hand_over(lock, aside)
            return
        if aside:
            self.held, self.error = False, ""
            self.reason = f"{self.name}: {aside}"
            return
        if not lock.held:
            lock.acquire()
        self.held = bool(lock.held)
        self.error = "" if self.held else lock.error
        if self.held:
            self.reason = f"holds {self.name} as {lock.holder} ({lock.kind})"
        elif self.error:
            self.reason = f"{self.name} not acquired: {self.error} ({lock.kind})"
        else:
            self.reason = f"{self.name} held by another process ({lock.kind})"

    def _hand_over(self, lock: KeeperLock, why: str) -> None:
        """A holder that stands aside: this round stops the workers (`held`
        False, the lease loop drains them); the next, once they stopped, gives
        the key up."""
        self.reason = f"{self.name}: {why}"
        if self.held:
            self.held = False
            log.warning("supervisor lease: %s; stopping the workers", self.reason)
            return
        lock.release()

    def advertised(self) -> str:
        """The lease this process's instance row names: '' unless it contends."""
        if self.mode != "auto" or self.given_up or _stand_aside_reason():
            return ""
        return self.name

    def remaining_s(self) -> float:
        """Seconds until this holder must count the lease lost: `lose_after`
        past the last renewal that landed. 0 when not held; unbounded outside
        `auto`."""
        if self.mode != "auto":
            return float("inf")
        lock = self._lock
        if lock is None or not self.held:
            return 0.0
        return float(lock.cfg.lose_after - (lock.clock() - lock.last_renew))

    def fresh(self) -> bool:
        """Held, and renewed recently enough to start a pass. No ClickHouse call."""
        return self.remaining_s() > 0

    def release(self) -> None:
        with self._mutex:
            self._release()

    def give_up(self, why: str) -> None:
        """Release the lease and stop contending for GIVE_UP_COOLDOWN_S: this
        process's workers cannot start. Another process takes over."""
        with self._mutex:
            self._release()
            self.given_up = why
            self._given_up_at = time.monotonic()
            self.reason = f"{self.name} given up: {why}"
        log.warning("supervisor lease: %s", self.reason)

    def _release(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock.ch.close()
            self._lock = None
        self.held = False


def _stand_aside_reason() -> str:
    """Why this process must not run the deployment's workers now ('' when it may)."""
    if readiness.draining():
        return "this process is draining"
    if found := deployment_config.drift():
        return "configuration differs from the deployment's: " + "; ".join(found)
    if reason := cell.gate_reason():
        return "this cell does not serve: " + reason
    return ""


def lease_config(ttl_s: int) -> LeaseConfig:
    """The supervisor lease's timings (the table in the module docstring)."""
    return LeaseConfig(ttl_s=ttl_s, wait_s=0, renew_after_s=ttl_s / 6, lose_after_s=ttl_s / 2)


def call_timeout_s(ttl_s: int) -> int:
    """Connect and read timeout of one lease statement. A renewal is two (the
    UPDATE and the read-back): one that hangs on both still leaves a retry
    before `lose_after` (from a TTL of 24 up). Liveness only: `keep_lease`'s
    deadline stops the workers whatever a call does."""
    return max(1, ttl_s // 24)


def _lock(name: str) -> KeeperLock:
    """The Keeper lock key."""
    from server.core.schema.migrate import client

    ttl = int(settings.supervisor_lease_ttl_s)
    short = call_timeout_s(ttl)
    ch = client(receive_timeout_s=short, connect_timeout_s=short, retries=False, session=False)
    try:
        return cluster_lock(ch, name=name, label="supervisor", cfg=lease_config(ttl), out=log.info)
    except Exception:
        ch.close()
        raise


async def decide_supervisor(mode: str) -> SupervisorLease:
    """The lease the lifespan starts from: `held` says whether to start the
    supervisor now. In `auto` mode the first attempt has been made."""
    lease = SupervisorLease(mode)
    try:
        await asyncio.to_thread(lease.step)
    except Exception as e:  # noqa: BLE001 — the lease loop retries; boot goes on
        log.warning("supervisor lease: %s", e)
    return lease


def drain_timeout_s() -> float:
    """How long a holder that lost the lease waits for its workers (T/6)."""
    return min(10.0, float(settings.supervisor_lease_ttl_s) / 6)


def _period_s(held: bool) -> float:
    """A holder steps every T/12 (renew_if_due renews at T/6, and retries a
    failed renewal at the next step); a contender every T/6."""
    ttl = float(settings.supervisor_lease_ttl_s)
    return max(1.0, ttl / 12 if held else ttl / 6)


async def keep_lease(
    lease: SupervisorLease,
    on_acquired: Callable[[], None],
    on_lost: Callable[[], Awaitable[None]],
) -> None:
    """Renew the lease while held, contend while not, and start or stop the
    supervisor when that changes. While the workers run, every wait (the
    sleep, a blocked renewal) is cut at the lease's deadline, and the workers
    stop then. `auto` mode, and a cell's `on` mode, whose gate is its only
    lease; cancelled on shutdown."""
    running = lease.held  # the lifespan started the supervisor when held at boot
    while True:
        running = await _until_done(asyncio.sleep(_period_s(lease.held)), lease, running, on_lost)
        step = asyncio.ensure_future(asyncio.to_thread(lease.step))
        running = await _until_done(step, lease, running, on_lost)
        if not step.cancelled() and (e := step.exception()) is not None:
            log.warning("supervisor lease: %s", e)
            continue
        if lease.held and not running and lease.fresh() and not readiness.draining():
            log.info("supervisor lease: acquired — %s", lease.reason)
            running = await _start(on_acquired, on_lost)
        elif running and not lease.held:
            log.warning("supervisor lease: lost — %s; stopping the workers", lease.reason)
            await _stop(on_lost)
            running = False


async def _until_done(
    aw: Awaitable[Any],
    lease: SupervisorLease,
    running: bool,
    on_lost: Callable[[], Awaitable[None]],
) -> bool:
    """Await `aw`; if the lease's deadline passes first while the workers run
    on a held lease, stop them and keep waiting. Returns whether they still run."""
    fut = asyncio.ensure_future(aw)
    try:
        while True:
            left = lease.remaining_s()
            # An `on` lease has no deadline (inf): wait without one.
            timeout = max(0.0, left) if running and lease.held and left != math.inf else None
            done, _ = await asyncio.wait({fut}, timeout=timeout)
            if done:
                return running
            log.warning(
                "supervisor lease: no renewal within %.0f s; stopping the workers",
                float(settings.supervisor_lease_ttl_s) / 2,
            )
            lease.reason = f"{lease.name}: renewal overdue; workers stopped"
            await _stop(on_lost)
            running = False
    finally:
        if not fut.done():
            fut.cancel()


async def _start(on_acquired: Callable[[], None], on_lost: Callable[[], Awaitable[None]]) -> bool:
    try:
        on_acquired()
        return True
    except Exception:  # the lease loop outlives a failed start; the next round retries
        log.exception("supervisor lease: starting the workers failed")
        await _stop(on_lost)  # whatever did start
        return False


async def _stop(on_lost: Callable[[], Awaitable[None]]) -> None:
    try:
        await on_lost()
    except Exception:  # the lease loop outlives a failed stop
        log.exception("supervisor lease: stopping the workers failed")


async def shutdown(lease: SupervisorLease, supervisor: Any) -> None:
    """Stop the workers whatever `held` says (one that did not drain after a
    lost lease still runs; a no-op when none do), then give the lease up, even
    when the shutdown budget cuts the stop short (a workers child is killed then)."""
    try:
        await supervisor.stop_all(drain_timeout_s=drain_timeout_s())
    finally:
        await asyncio.to_thread(lease.release)


_CURRENT: WorkersLease | None = None


# The last read of the lease row: (monotonic time, some process holds it live).
_holder_read: tuple[float, bool] | None = None


async def read_holder(ch: Any) -> None:
    """Record whether some process holds the workers lease and its renewal has
    not lapsed, on the store's clock. Run on each instance beat."""
    global _holder_read
    _holder_read = (time.monotonic(), await has_live_holder(LEASE_NAME, ch=ch))


def holder_live() -> bool | None:
    """The last `read_holder` answer while it is as recent as a live instance
    row, else None."""
    if _holder_read is None or time.monotonic() - _holder_read[0] > instances.live_window_s():
        return None
    return _holder_read[1]


def set_current(lease: WorkersLease) -> None:
    global _CURRENT
    _CURRENT = lease


def current() -> WorkersLease | None:
    return _CURRENT


def leads_deployment() -> bool:
    """This process runs the deployment's workers, so the leader's duties too:
    the retention ALTERs, the tracker namespace rows, the published
    configuration and the provider participant sweep. Never outside the
    deployment, whatever its mode."""
    lease = current()
    return bool(settings.deployment) and lease is not None and lease.held


def advertised_lease() -> str:
    """The lease this process's instance row names ('' when it does not contend)."""
    lease = current()
    return lease.advertised() if isinstance(lease, SupervisorLease) else ""


__all__ = [
    "GIVE_UP_COOLDOWN_S",
    "LEASE_NAME",
    "SupervisorLease",
    "WorkersLease",
    "advertised_lease",
    "call_timeout_s",
    "current",
    "decide_supervisor",
    "drain_timeout_s",
    "holder_live",
    "keep_lease",
    "lease_config",
    "leads_deployment",
    "newer_build",
    "read_holder",
    "set_current",
    "shutdown",
]
