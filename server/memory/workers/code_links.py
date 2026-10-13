"""Code link pass: link the memories written since the last pass to the code they name
(and, on the first pass or after falling behind the queue, the ones written before),
then follow every repository's links to its indexed head and run the memories' `revise`
policy (`server.memory.app.code_links`, `code_link_recheck`)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from server.memory.store.key_write_lock import LeaseBusy, LeaseLost
from server.workers.status import log_pass_errors, record_pass

if TYPE_CHECKING:
    from server.memory.app.code_link_pass import PassResult

log = logging.getLogger(__name__)

# queue pages read a pass, each `memory_code_links_batch` long, while they come full
_QUEUE_PAGES = 10


async def run_pass() -> dict[str, Any]:
    from server.code.app import memory_links as code
    from server.memory.app import code_link_pass, code_link_recheck, code_links
    from server.settings import settings

    batch = int(settings.memory_code_links_batch)
    result = code_link_pass.PassResult()
    with record_pass("memory_code_links") as rec:
        await code.refresh_repos()
        try:
            # one host reads the queue at a time: two would link the same writes twice
            async with code_links.queue_lease():
                await _link_writes(batch, result)
        except LeaseBusy:
            pass  # another host is linking them
        except LeaseLost as e:  # another host took them over: their cursors are written
            result.errors.append(f"link: {e!r}")
        await code_link_recheck.recheck_all(result)
        rec.outcome = result.as_dict()
        log_pass_errors(log, "memory_code_links", result.errors)
    return result.as_dict()


async def _link_writes(batch: int, result: PassResult) -> None:
    """Link the queued writes, the backfill's next page, and sweep erased memories."""
    from server.code.app import memory_links as code
    from server.memory.app import code_link_pass, code_links
    from server.memory.store import code_links as store

    queue_cursor = code_link_pass.Cursor[list[Any] | None]("queue", None)
    backfill_cursor = code_link_pass.Cursor[dict[str, Any]]("backfill", {})
    repos_cursor = code_link_pass.Cursor[list[list[str]]]("repos", [])
    sweep_cursor = code_link_pass.Cursor[dict[str, Any]]("sweep", {})
    queue = await queue_cursor.read()
    stored = backfill = await backfill_cursor.read()
    if queue is None:
        # first pass: the queue starts at its tail; everything before it is backfilled
        queue, backfill = list(await store.queue_tail() or []), {}
    behind = bool(queue) and int(queue[0]) < await store.queue_horizon()
    if behind and not backfill.get("stall"):
        # stopped longer than the queue's TTL: writes may have aged out, before or after
        # where a scan in progress is; a fresh scan, once while the queue catches up
        backfill = {"stall": True}
    elif not behind and backfill.get("stall"):
        backfill = {k: v for k, v in backfill.items() if k != "stall"}  # caught up
    stall = {"stall": True} if backfill.get("stall") else {}
    configured = code.configured_code_namespaces()
    if {tuple(r) for r in configured} - {tuple(r) for r in await repos_cursor.read()}:
        backfill = stall  # a repository configured since: memories read before it now resolve
    if backfill != stored:
        # owed before the queue or the repositories are acknowledged: a pass that fails
        # after either still owes it
        await backfill_cursor.write(backfill)
    await repos_cursor.write(configured)
    if configured:  # with no repository nothing links; one configured restarts the backfill
        position = tuple(queue) or None
        for _ in range(_QUEUE_PAGES):
            read = result.queued
            position = await code_links.link_queued(position, batch, result)
            await queue_cursor.write(list(position or []))
            if result.queued - read < batch:
                break
        if not backfill.get("done"):
            after = tuple(backfill.get("after") or ()) or None
            nxt = await code_links.backfill(after, batch, result)
            backfill = {"done": True, **stall} if nxt is None else {"after": list(nxt), **stall}
        await backfill_cursor.write(backfill)
    swept = await code_links.sweep_erased(await sweep_cursor.read(), batch, result)
    await sweep_cursor.write(swept)


__all__ = ["run_pass"]
