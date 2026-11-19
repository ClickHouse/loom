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
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from time import monotonic, time_ns
from typing import Any
from uuid import UUID

from server.core import pg, pg_profile, retirement
from server.core.background import drain_task_sets, flush_registered_buffers
from server.core.ch import query_tag
from server.core.model_work_gate import get_model_work_gate
from server.core.provider_process import (
    LIFECYCLE,
    has_certificate,
    participant_proof,
    process_identity,
    proves_stopped,
)
from server.core.repo_base import _default_client
from server.insights.store.provider_participants import (
    OWN_VERSION_SQL,
    SEQUENTIAL,
    STALE_AFTER_S,
    TABLE,
    participants,
    prune_retired,
    record_participant,
    record_retirement,
    running_queries,
)
from server.settings import settings

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
                if self.state == "retired" and state != "retired":
                    raise retirement.ProcessStopping("A retired UUID cannot re-enroll")
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
        row = [me, transition, state, version, STALE_AFTER_S, participant_proof()]
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
_process_pid = os.getpid()


async def register_process() -> None:
    """Enroll before loading settings or admitting any workers/requests.

    A coordinator may miss a newborn in its census, but it cannot write yet:
    startup loads the pending gate or the committed replacement before admission.
    A second call (boot enrolls before its migrations) keeps the state.
    """
    global _registered, _process_pid, _OWN, _HEARTBEAT
    if _process_pid != os.getpid():
        _process_pid = os.getpid()
        _registered, _OWN, _HEARTBEAT = False, _Own(), _Heartbeat()
        from server.core import cell

        cell.discard_inherited_view()
    if retirement.get_gate().closed:
        raise retirement.ProcessStopping("A retired participant cannot re-enroll")
    if not _registered:
        _OWN.stamp(UUID(int=0), "running")
    await asyncio.to_thread(_HEARTBEAT.start)
    _registered = True
    retirement.get_gate().enrolled = True


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


async def _server_side_settled(*, ch: Any = None) -> bool:
    """No write of this process can still land: no carried insert in flight or
    pinned (a cancelled one may still run or start), and no insert of its own
    running on any replica (a cancelled request's insert outlives its task)."""
    from server.core import rmt_carry

    if rmt_carry.unsettled():
        return False
    return await running_queries(str(process_identity()), ch=ch, inserts=True) == 0


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


async def _authority_settled(
    ch: Any, participant: UUID, proof: str, *, end: float, own: bool = False
) -> bool:
    """Fresh authority selection and positive absence, under one caller-owned end."""
    try:
        async with asyncio.timeout_at(end):
            recorded = await pg_profile.recorded_authority(ch)
            if (
                recorded is None
                or recorded.get("authority") not in ("clickhouse", "postgres")
                or recorded["authority"] != settings.storage_authority
                or type(recorded.get("epoch")) is not int
                or recorded["epoch"] <= 0
                or UUID(str(recorded.get("deployment_id"))).int == 0
            ):
                return False
            parts = proof.split("|")
            if len(parts) == 9:
                if parts[8] != LIFECYCLE or not has_certificate(proof):
                    return False
                legacy = False
            elif len(parts) in (6, 7, 8):
                legacy = True
            else:
                return False
            if recorded["authority"] == "clickhouse":
                return not own or not pg.authority_state_present()
            if not settings.pg_dsn:
                return False
            return await pg.retirement_settled(participant, recorded, legacy=legacy, end=end)
    except Exception:  # noqa: BLE001 — missing or inconsistent authority evidence refuses retirement.
        return False


async def retire_process(ch: Any, *, end: float) -> bool:
    """Certify permanent local ownership and both stores' positive settlement."""
    gate = retirement.get_gate()
    gate.close()
    _HEARTBEAT.signal_stop()
    async with asyncio.timeout_at(end):
        if not gate.quiescent or get_model_work_gate().active:
            return False
        if not await drain_task_sets(timeout_s=0):
            return False
        if not gate.pg_sealed:
            if not await flush_registered_buffers() or not await drain_task_sets(timeout_s=0):
                return False
            if not gate.quiescent or get_model_work_gate().active:
                return False
            gate.pg_sealed = True
        # A CH authority must not silently discard contradictory local PG ownership.
        if settings.storage_authority == "clickhouse" and pg.authority_state_present():
            return False
        if not await pg.close_pool_until(end=end) or not gate.quiescent:
            return False
        if not await _authority_settled(
            ch, process_identity(), participant_proof(), end=end, own=True
        ):
            return False
        if not await _server_side_settled(ch=ch):
            return False
        if _HEARTBEAT.running or await running_queries(str(process_identity()), ch=ch) != 0:
            return False
        if not gate.quiescent or get_model_work_gate().active:
            return False
        _, _, version = _OWN.stamp(UUID(int=0), "retired")
        proof = retirement.guarded_proof(process_identity(), version=version)
        with query_tag(_TAG["log_comment"]):
            await record_retirement(proof, ch=ch)
        pg.finish_retirement()
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

    try:
        return await _retire_until(ch, monotonic() + budget_s)
    except TimeoutError:
        log.warning(
            "provider participant not retired at shutdown: %d carried write(s) unsettled "
            "or an insert of this process still running; retire it with "
            "`loom participant retire --participant %s` on its trusted host once it stopped",
            rmt_carry.unsettled(),
            process_identity(),
        )
    except Exception as e:  # noqa: BLE001 — the row then expires; health names it
        log.warning("provider participant not retired at shutdown: %s", e)
    return False


async def _retire_until(ch: Any, deadline: float) -> bool:
    """Retry the existing settlement checks under the caller's absolute deadline."""
    from server.core import rmt_carry

    async with asyncio.timeout_at(deadline):
        while True:
            if await retire_process(ch, end=deadline):
                return True
            with contextlib.suppress(Exception):
                await rmt_carry.settle(ch)
            await asyncio.sleep(min(1.0, max(0.0, deadline - monotonic())))


def participant_lifespan[T](
    lifespan: Callable[[T], AbstractAsyncContextManager[None]],
) -> Callable[[T], AbstractAsyncContextManager[None]]:
    """A failed boot stops known producers but never certifies an incomplete shutdown."""

    @asynccontextmanager
    async def wrapped(app: T) -> AsyncIterator[None]:
        started = False
        try:
            async with lifespan(app):
                started = True
                yield
        except BaseException:
            if not started and _registered:
                _HEARTBEAT.signal_stop()
                gate = retirement.get_gate()
                gate.close()
                for producer in tuple(gate.producers):
                    producer.cancel()
            raise

    return wrapped


async def retire_reaped_process(host_proof: str, end: float) -> None:
    """Retire only the one recorded process matching its owner's captured birth."""
    ch = _default_client()
    async with asyncio.timeout_at(end):
        rows = await participants(ch=ch, host_proof=host_proof)
        if len(rows) != 1 or rows[0].get("host_proof") != host_proof:
            return
        await retire_participant(
            UUID(str(rows[0]["participant_id"])), ch=ch, expected_proof=host_proof, end=end
        )


async def _retire_dead_row(ch: Any, row: dict[str, Any], *, end: float) -> bool:
    participant = UUID(str(row["participant_id"]))
    physical = str(row.get("host_proof") or "")
    if str(participant) == str(process_identity()) or not proves_stopped(physical):
        return False
    if not await _authority_settled(ch, participant, physical, end=end):
        return False
    if await running_queries(participant, ch=ch) != 0:
        return False
    proof = retirement.death_proof(participant, physical, after=int(row["version"]))
    if proof is None:
        return False
    await record_retirement(proof, ch=ch)
    return True


async def retire_participant(
    participant: UUID, *, ch: Any = None, expected_proof: str | None = None, end: float
) -> str:
    """Retire only exact physical death with both stores positively settled."""
    client = ch if ch is not None else _default_client()
    try:
        async with asyncio.timeout_at(end):
            rows = (
                await participants(ch=client)
                if expected_proof is None
                else await participants(ch=client, host_proof=expected_proof)
            )
            row = next((r for r in rows if str(r["participant_id"]) == str(participant)), None)
            if row is None:
                return "absent"
            if expected_proof is not None and (
                len(rows) != 1 or row.get("host_proof") != expected_proof
            ):
                return "live"
            return "retired" if await _retire_dead_row(client, row, end=end) else "live"
    except TimeoutError:
        return "live"


_last_here = -float("inf")
HERE_S = 10.0


async def retire_dead_here(*, end: float, now: float | None = None) -> int:
    """Exact local deaths still need positive settlement of both stores."""
    global _last_here
    t = monotonic() if now is None else now
    if t - _last_here < HERE_S:
        return 0
    _last_here = t
    retired = 0
    ch = _default_client()
    async with asyncio.timeout_at(end):
        for row in await participants(ch=ch):
            if await _retire_dead_row(ch, row, end=end):
                log.info(
                    "provider participant %s retired: its process on this host is gone",
                    row["participant_id"],
                )
                retired += 1
    return retired


# Participants the last sweep could not retire, for /v1/health; None before
# it (and off the workers leader, which alone sweeps).
_STALE: tuple[dict[str, str], ...] | None = None
_last_sweep = 0.0
_last_prune: float | None = None


def stale_participants() -> tuple[dict[str, str], ...] | None:
    return _STALE


async def sweep_stopped(*, end: float, now: float | None = None) -> None:
    """Remember stale blockers; exact death and both stores authorize retirement."""
    global _STALE, _last_sweep, _last_prune
    t = monotonic() if now is None else now
    if _STALE is not None and t - _last_sweep < SWEEP_S:
        return
    _last_sweep = t
    ch = _default_client()
    async with asyncio.timeout_at(end):
        if _last_prune is None or t - _last_prune >= PRUNE_S:
            _last_prune = t
            await prune_retired(ch=ch)
        me = str(process_identity())
        stale: list[dict[str, str]] = []
        for row in await participants(ch=ch):
            participant = str(row["participant_id"])
            if participant == me:
                continue
            entry = {
                "participant_id": participant,
                "heartbeat_at": str(row.get("heartbeat_at", "")),
            }
            if row.get("expired"):
                if await _retire_dead_row(ch, row, end=end):
                    continue
                running = await running_queries(participant, ch=ch) != 0
                stale.append(
                    {**entry, "reason": "queries_running" if running else "heartbeat_expired"}
                )
        _STALE = tuple(stale)
