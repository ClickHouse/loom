"""Permanent process admission and the evidence passed to the retirement writer.

Model maintenance can reopen. This gate never does: the participant identity is
finished once shutdown begins, even when its shutdown budget expires.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from server.core import provider_process


class ProcessStopping(RuntimeError):
    """This participant can never accept another unit of work."""


class Uncertifiable(RuntimeError):
    """This process settled its writes but cannot itself supply evidence its
    retirement needs: its row stays, and a provider change waits for it."""


# Marks, as an exception note, a failed shutdown of a command whose body returned.
UNFINISHED = "the command returned; its shutdown did not complete"


def unfinished(error: BaseException) -> bool:
    """Whether `error` carries `UNFINISHED`: `loom` prints one line for it."""
    return UNFINISHED in getattr(error, "__notes__", ())


class RetirementGate:
    """One event loop; checking admission and incrementing never await."""

    def __init__(self) -> None:
        self.active = 0
        self.closed = False
        self.pg_sealed = False
        self.role = ""
        self.enrolled = False
        self.producers_stopped = False
        self.producers: set[asyncio.Task[Any]] = set()
        # Native completion needs the first failed producer after its Task is discarded.
        # Fresh gates, including fork reset, discard this one command's outcome.
        self.producer_failure: BaseException | None = None

    def install(self, role: str) -> None:
        if self.closed or self.role not in ("", role):
            raise ProcessStopping("A retired participant cannot enroll again")
        self.role = role

    def close(self) -> None:
        self.closed = True

    def track[T](self, task: asyncio.Task[T]) -> asyncio.Task[T]:
        """A process-lifetime producer must actually end, not merely be canceled."""
        self.producers.add(task)
        task.add_done_callback(self._producer_done)
        if self.closed:
            task.cancel()
            raise ProcessStopping("Cannot start a producer during shutdown")
        return task

    def _producer_done(self, task: asyncio.Task[Any]) -> None:
        self.producers.discard(task)
        if self.role == "command" and not task.cancelled():
            error = task.exception()
            if self.producer_failure is None:
                self.producer_failure = error

    def stopped(self) -> None:
        """Composition calls this after stopping every role-specific producer."""
        self.close()
        self.producers_stopped = True

    @property
    def quiescent(self) -> bool:
        return (
            self.closed
            and bool(self.role)
            and self.producers_stopped
            and self.active == 0
            and all(task.done() for task in self.producers)
        )

    @asynccontextmanager
    async def admit(self) -> AsyncIterator[None]:
        if self.closed or (self.role and not self.enrolled):
            raise ProcessStopping("This process is stopping or has not enrolled")
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1


_pid = os.getpid()
_gate = RetirementGate()


def get_gate() -> RetirementGate:
    """A fork cannot inherit an accepting view or its parent's retirement state."""
    global _pid, _gate
    if _pid != os.getpid():
        role = _gate.role
        _pid, _gate = os.getpid(), RetirementGate()
        _gate.role = role
    return _gate


@dataclass(frozen=True)
class RetirementProof:
    """Internal evidence; only guarded shutdown or verified death constructs it."""

    participant: UUID
    host_proof: str
    after: int
    version: int | None = None


def guarded_proof(participant: UUID, *, version: int) -> RetirementProof:
    gate = get_gate()
    proof = provider_process.host_proof()
    if not gate.quiescent or not gate.pg_sealed or not gate.enrolled or not proof:
        raise ProcessStopping("Permanent guarded quiescence is not established")
    return RetirementProof(participant, provider_process.certified(proof), version - 1, version)


def death_proof(participant: UUID, proof: str, *, after: int) -> RetirementProof | None:
    """Called only after the complete cross-replica running-query census is zero."""
    if not provider_process.proves_stopped(proof):
        return None
    return RetirementProof(participant, provider_process.certified(proof), after)
