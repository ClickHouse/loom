"""Finish the checks memory.delete_version owes after its tombstone.

Runs the checks a memory.delete_version 503 left owed, under the key lock, as the delete
would have (`handlers.rerun_delete_check`). Running a check twice is harmless.
"""

from __future__ import annotations

import logging

from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


async def run_pass() -> int:
    """Run the owed checks one pass takes; the number finished."""
    import server.memory.store as memory_repo
    from server.memory.app.handlers import rerun_delete_check
    from server.memory.store import key_write_lock

    with record_pass("delete_version_check") as rec:
        owed = await memory_repo.pending_delete_checks(
            older_than_seconds=settings.delete_version_check_grace_seconds,
            limit=settings.delete_version_check_batch_size,
        )
        finished = 0
        for check in owed:
            try:
                async with key_write_lock.key_write_locks.hold(
                    check["org"],
                    check["namespace"],
                    check["agent"],
                    check["user_id"],
                    check["session_id"],
                    check["memory_key"],
                ):
                    await rerun_delete_check(check)
                finished += 1
            except Exception as e:  # noqa: BLE001 — one key's check must not stop the others
                pass_context.note_failure(e, "delete_version_check")
        rec.outcome = {"owed": len(owed), "finished": finished}
    if not owed:
        pass_context.mark_idle()
    elif finished:
        log.info("delete_version_check: finished %d of %d owed checks", finished, len(owed))
    return finished
