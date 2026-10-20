"""Distributed admission census.

A participant counts as stopped only by its own retirement, an acknowledgement
of the transition, a later process on its host that proves it gone
(`retire_dead_here`), or an operator's retirement (`loom participant retire`).
An expired heartbeat proves nothing: a paused process can hold admitted work
outside ClickHouse and write it with the old configuration once it resumes, so
it blocks a reconfiguration until it beats again and drains, or is retired.
"""

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from time import monotonic, time_ns
from typing import Any
from uuid import UUID

from server.core.background import drain_task_sets, flush_registered_buffers
from server.core.ch import query_tag
from server.core.model_work_gate import get_model_work_gate
from server.core.provider_process import host_proof, pod_gone, process_identity, proves_stopped
from server.insights.store.provider_participants import (
    OWN_VERSION_SQL,
    SEQUENTIAL,
    STALE_AFTER_S,
    TABLE,
    participants,
    prune_retired,
    record_participant,
    running_queries,
)

log = logging.getLogger(__name__)

# Every participant writes its row this often, from a thread of its own, so a
# boot migration or a blocked event loop does not stop it.
HEARTBEAT_S = 10.0
# How often the workers leader retires the stopped participants it finds,
# and deletes the long-retired ones.
SWEEP_S = 60.0
PRUNE_S = 3600.0

_COLUMNS = ["participant_id", "transition_id", "state", "version", "stale_after_s", "host_proof"]
_TAG = {"log_comment": "provider.participant_heartbeat"}


class _Own:
    """This process's row. A write takes its state and version under one lock,
    so the highest version always carries the latest state."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.transition = UUID(int=0)
        self.state = "running"
        self.version = 0

    def stamp(
        self, transition: UUID | None = None, state: str | None = None, *, floor: int = 0
    ) -> tuple[UUID, str, int]:
        with self.lock:
            if transition is not None and state is not None:
                self.transition, self.state = transition, state
            self.version = max(time_ns(), self.version + 1, floor + 1)
            return self.transition, self.state, self.version


_OWN = _Own()


def _connect() -> Any:
    from server.core.schema.migrate import client

    return client(receive_timeout_s=15, connect_timeout_s=5, retries=False)


class _Heartbeat:
    """Writes this process's row every `HEARTBEAT_S` on its own connection."""

    def __init__(self, connect: Callable[[], Any] = _connect) -> None:
        self.connect = connect
        self.ch: Any = None
        self.thread: threading.Thread | None = None
        self.stopping = threading.Event()

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        """Write the first row now (raising when it does not land), then beat."""
        if self.running:
            return
        self.stopping.clear()
        if self.ch is None:
            self.ch = self.connect()
        self.beat()
        self.thread = threading.Thread(target=self._run, name="provider-participant", daemon=True)
        self.thread.start()

    def beat(self) -> None:
        me = process_identity()
        rows = self.ch.query(
            OWN_VERSION_SQL, parameters={"id": str(me)}, settings={**SEQUENTIAL, **_TAG}
        ).result_rows
        seen = int(rows[0][0] or 0) if rows else 0
        transition, state, version = _OWN.stamp(floor=seen)
        if state == "retired":
            return
        row = [me, transition, state, version, STALE_AFTER_S, host_proof()]
        self.ch.insert(TABLE, [row], column_names=_COLUMNS, settings=_TAG)

    def _run(self) -> None:
        while not self.stopping.wait(HEARTBEAT_S):
            try:
                if self.ch is None:
                    self.ch = self.connect()
                self.beat()
            except Exception as e:  # noqa: BLE001 — a missed beat retries; expiry takes several
                log.warning("provider participant heartbeat failed: %s", str(e)[:200])
                self._close()
        self._close()

    def _close(self) -> None:
        ch, self.ch = self.ch, None
        if ch is not None:
            try:
                ch.close()
            except Exception:  # noqa: BLE001 — a dead connection has nothing to close
                pass

    def stop(self) -> None:
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(timeout=HEARTBEAT_S + 20)
            self.thread = None

    def signal_stop(self) -> None:
        """Stop beating without waiting for the thread (a daemon, it ends after its
        current beat): a join could outlast the shutdown, and a beat that loses the
        race finds the row retired, or writes below its version."""
        self.stopping.set()


_HEARTBEAT = _Heartbeat()
_registered = False


async def register_process() -> None:
    """Enroll before loading settings or admitting any workers/requests.

    A coordinator may miss a newborn in its census, but it cannot write yet:
    startup loads the pending gate or the committed replacement before admission.
    A second call (boot enrolls before its migrations) keeps the state.
    """
    global _registered
    if not _registered:
        _OWN.stamp(UUID(int=0), "running")
    await asyncio.to_thread(_HEARTBEAT.start)
    _registered = True


async def enroll_before_migrations(ch: Any) -> None:
    """A reconfiguration activated while this process migrates then waits for it.
    Skipped on a database whose migrations stopped before the heartbeat columns:
    the heartbeat could not land, and the boot enrolls after migrating anyway."""
    row = await ch.query_one(
        "SELECT count() AS n FROM system.columns "
        "WHERE database = currentDatabase() AND table = '_provider_participants' "
        "AND name = 'host_proof'"
    )
    if row is not None and row.get("n") == 1:
        await register_process()


async def _server_side_settled() -> bool:
    """No write of this process can still land: no carried insert in flight or
    pinned (a cancelled one may still run or start), and no insert of its own
    running on any replica (a cancelled request's insert outlives its task)."""
    from server.core import rmt_carry

    if rmt_carry.unsettled():
        return False
    return await running_queries(str(process_identity()), inserts=True) == 0


async def acknowledge_drain(transition: UUID) -> bool:
    gate = get_model_work_gate()
    gate.close(str(transition))
    if not await gate.wait_drained(str(transition), timeout_s=0):
        return False
    if not await drain_task_sets(timeout_s=0):
        return False
    # Flush buffered ledgers after all registered producers finished. The boot
    # drain's bounded waits alone are not proof: take another task census.
    if not await flush_registered_buffers():
        return False
    if not await drain_task_sets(timeout_s=0):
        return False
    if not await _server_side_settled():
        return False
    _, _, version = _OWN.stamp(transition, "drained")
    await record_participant(process_identity(), transition, "drained", version=version)
    return True


async def distributed_drain(transition: UUID) -> bool:
    """Caller serializes every effect with configuration_lock; newcomers start closed."""
    rows = await participants()
    me = str(process_identity())
    if not rows or not any(str(r["participant_id"]) == me for r in rows):
        return False
    return all(r["state"] == "drained" and str(r["transition_id"]) == str(transition) for r in rows)


async def retire_process() -> bool:
    """Retire only when no admitted or registered deferred writer remains, and no
    write of this process can still land on the server."""
    global _registered
    if get_model_work_gate().active or not await drain_task_sets(timeout_s=0):
        return False
    if not await _server_side_settled():
        return False
    _HEARTBEAT.signal_stop()
    _, _, version = _OWN.stamp(UUID(int=0), "retired")
    with query_tag(_TAG["log_comment"]):
        await record_participant(process_identity(), UUID(int=0), "retired", version=version)
    _registered = False
    return True


# How long shutdown keeps trying to retire beyond what the drains left unused
# (`boot.SHUTDOWN_DRAIN_BUDGET_S`): the ready delay, uvicorn's graceful timeout, the
# drains and this stay inside the 75 s stop grace, with room to withdraw and disconnect.
SHUTDOWN_RETIRE_S = 7.0


async def retire_at_shutdown(ch: Any, *, budget_s: float = SHUTDOWN_RETIRE_S) -> bool:
    """Retire this process, retrying while a cut-off write may still land: a process
    that exits unretired blocks every reconfiguration until an operator retires it.
    Every attempt, settle and pause ends by `budget_s` from now."""
    from server.core import rmt_carry

    deadline = monotonic() + budget_s
    try:
        async with asyncio.timeout(budget_s):
            while True:
                if await retire_process():
                    return True
                with contextlib.suppress(Exception):
                    await rmt_carry.settle(ch)
                await asyncio.sleep(min(1.0, max(0.0, deadline - monotonic())))
    except TimeoutError:
        log.warning(
            "provider participant not retired at shutdown: %d carried write(s) unsettled "
            "or an insert of this process still running; retire it with "
            "`loom participant retire --participant %s --yes` once it stopped",
            rmt_carry.unsettled(),
            process_identity(),
        )
    except Exception as e:  # noqa: BLE001 — the row then expires; health names it
        log.warning("provider participant not retired at shutdown: %s", e)
    return False


def participant_lifespan[T](
    lifespan: Callable[[T], AbstractAsyncContextManager[None]],
) -> Callable[[T], AbstractAsyncContextManager[None]]:
    """A boot that fails after enrolling retires its row instead of leaving one per attempt."""

    @asynccontextmanager
    async def wrapped(app: T) -> AsyncIterator[None]:
        started = False
        try:
            async with lifespan(app):
                started = True
                yield
        except BaseException:
            if not started and _registered:
                try:
                    await retire_process()
                except Exception as e:  # noqa: BLE001 — the row expires by heartbeat instead
                    log.warning("provider participant not retired after failed boot: %s", e)
            raise

    return wrapped


async def retire_participant(participant: UUID, *, ch: Any = None) -> str:
    """`retired`, `absent` (no live row), or `live`: its heartbeat is fresh or a
    query tagged with its id still runs."""
    row = next(
        (r for r in await participants(ch=ch) if str(r["participant_id"]) == str(participant)),
        None,
    )
    if row is None:
        return "absent"
    if str(participant) == str(process_identity()):
        return "live"
    if not row.get("expired"):
        return "live"
    if await running_queries(participant, ch=ch) != 0:
        return "live"
    await record_participant(participant, UUID(int=0), "retired", after=int(row["version"]), ch=ch)
    return "retired"


_last_here = -float("inf")
HERE_S = 10.0


async def retire_dead_here(*, now: float | None = None) -> int:
    """Retire the participants this host proves stopped (`proves_stopped`: a
    restarted container or a respawned worker) with no query of theirs left
    running. Every process's job, every `HERE_S`; how many it retired."""
    global _last_here
    t = monotonic() if now is None else now
    if t - _last_here < HERE_S:
        return 0
    _last_here = t
    me = str(process_identity())
    retired = 0
    for row in await participants():
        participant = str(row["participant_id"])
        if participant == me or not proves_stopped(str(row.get("host_proof") or "")):
            continue
        if await running_queries(participant) != 0:
            continue
        await record_participant(
            UUID(participant), UUID(int=0), "retired", after=int(row["version"])
        )
        log.info("provider participant %s retired: its process on this host is gone", participant)
        retired += 1
    return retired


# Participants the last sweep could not retire, for /v1/health; None before
# it (and off the workers leader, which alone sweeps).
_STALE: tuple[dict[str, str], ...] | None = None
_last_sweep = 0.0
_last_prune: float | None = None


def stale_participants() -> tuple[dict[str, str], ...] | None:
    return _STALE


async def sweep_stopped(*, now: float | None = None) -> None:
    """Retire the expired participants with no query left whose Kubernetes pod is
    gone (`pod_gone`), and remember the rest that block a reconfiguration (only
    their process, Kubernetes or an operator can say they stopped). The workers
    leader's job."""
    global _STALE, _last_sweep, _last_prune
    t = monotonic() if now is None else now
    if _STALE is not None and t - _last_sweep < SWEEP_S:
        return
    _last_sweep = t
    if _last_prune is None or t - _last_prune >= PRUNE_S:
        _last_prune = t
        await prune_retired()
    me = str(process_identity())
    stale: list[dict[str, str]] = []
    for row in await participants():
        participant = str(row["participant_id"])
        if participant == me:
            continue
        entry = {"participant_id": participant, "heartbeat_at": str(row.get("heartbeat_at", ""))}
        if row.get("expired"):
            running = await running_queries(participant) != 0
            if not running and await pod_gone(str(row.get("host_proof") or "")):
                await record_participant(
                    UUID(participant), UUID(int=0), "retired", after=int(row["version"])
                )
                log.info("provider participant %s retired: Kubernetes deleted its pod", participant)
                continue
            stale.append({**entry, "reason": "queries_running" if running else "heartbeat_expired"})
    _STALE = tuple(stale)
