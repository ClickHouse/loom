"""Finish the checks memory.delete_version owes after its tombstone.

A delete records its check before the tombstone and marks it done once it ran. One
left open (the read after the tombstone failed) is run here, under the key lock, as
the delete would have run it: a write that landed between the version check and the
tombstone is written back above it, and what the removed version derived retires
only when the delete stands. Running a check twice is harmless.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from server.settings import settings
from server.workers import pass_context
from server.workers.status import record_pass

log = logging.getLogger(__name__)


async def run_pass() -> int:
    """Run the owed checks one pass takes; the number finished."""
    import server.memory.store as memory_repo
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
                    await _finish(check)
                finished += 1
            except Exception as e:  # noqa: BLE001 — one key's check must not stop the others
                pass_context.note_failure(e, "delete_version_check")
        rec.outcome = {"owed": len(owed), "finished": finished}
    if not owed:
        pass_context.mark_idle()
    elif finished:
        log.info("delete_version_check: finished %d of %d owed checks", finished, len(owed))
    return finished


async def _finish(check: dict[str, Any]) -> None:
    """One owed check: run it when its tombstone is in and the removed version is still
    readable; otherwise there is nothing to check, and it is closed."""
    import server.memory.store as memory_repo
    from server.memory.app.handlers import finish_delete_check

    checked, tombstone = int(check["checked"]), int(check["tombstone"])
    ident = (check["agent"], check["user_id"], check["session_id"])
    rows = await memory_repo.fetch_version_rows(
        check["org"],
        check["namespace"],
        [(check["memory_key"], checked), (check["memory_key"], tombstone)],
        sequential=True,
    )
    mine = {r.version: r for r in rows if (r.agent, r.user_id, r.session_id) == ident}
    removed, landed = mine.get(checked), mine.get(tombstone)
    if removed is not None and landed is not None and landed.tombstoned_at is not None:
        await finish_delete_check(removed, checked=checked, tombstone=tombstone)
        return
    # The tombstone never landed (the delete failed after recording its check), or the
    # rows were erased since: nothing to check.
    await memory_repo.record_delete_check(
        SimpleNamespace(
            **{
                k: check[k]
                for k in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
            }
        ),
        checked=checked,
        tombstone=tombstone,
        done=True,
    )
