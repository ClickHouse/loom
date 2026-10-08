"""Distributed admission census; no heartbeat expiry is a drain acknowledgement."""

from uuid import UUID

from server.core.background import drain_task_sets, flush_registered_buffers
from server.core.model_work_gate import get_model_work_gate
from server.core.provider_process import process_identity, stopped_process
from server.insights.store.provider_participants import (
    participants,
    record_participant,
    storage_idle,
)


async def register_process() -> None:
    """Enroll before loading settings or admitting any workers/requests.

    A coordinator may miss a newborn in its census, but it cannot write yet:
    startup loads the pending gate or the committed replacement before admission.
    Existing participants never disappear by timeout.
    """
    await record_participant(process_identity(), UUID(int=0), "running")


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
    await record_participant(process_identity(), transition, "drained")
    return True


async def distributed_drain(transition: UUID) -> bool:
    """Caller serializes every effect with configuration_lock; newcomers start closed."""
    rows = await participants()
    if not rows or not any(str(r["participant_id"]) == str(process_identity()) for r in rows):
        return False
    for row in rows:
        if row["state"] == "drained" and str(row["transition_id"]) == str(transition):
            continue
        with stopped_process(str(row["participant_id"])) as stopped:
            if not stopped or not await storage_idle():
                return False
            await record_participant(
                UUID(str(row["participant_id"])), transition, "retired", after=int(row["version"])
            )
    # Even a crashed producer's acknowledged query can outlive its socket.
    return await storage_idle()


async def retire_process() -> bool:
    """Retire only when no admitted or registered deferred writer remains."""
    if get_model_work_gate().active or not await drain_task_sets(timeout_s=0):
        return False
    await record_participant(process_identity(), UUID(int=0), "retired")
    return True
