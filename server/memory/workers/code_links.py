"""Code link pass: link the memories written since the last pass to the code they name
(and, on the first pass or after falling behind the queue, the ones written before),
then follow every repository's links to its indexed head and run the memories' `revise`
policy (`server.memory.app.code_links`, `code_link_recheck`)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from server.memory.store.key_write_lock import LeaseBusy
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
        try:
            await code.refresh_repos()
            # one host reads the queue at a time: two would link the same writes twice
            async with code_links.queue_lease():
                await _link_writes(batch, result)
        except LeaseBusy:
            pass  # another host is linking them
        except Exception as e:  # a linking failure never stops the recheck
            # LeaseLost among them: the queue lease lapsed; the next pass reads on from the
            # cursors last written
            log.exception("memory_code_links: linking failed")
            result.errors.append(f"link: {e!r}")
        await code_link_recheck.recheck_all(result)
        rec.outcome = result.as_dict()
        log_pass_errors(log, "memory_code_links", result.errors)
    return result.as_dict()


@dataclass(frozen=True)
class Backfill:
    """The backfill cursor: the scan of the memories the queue did not deliver, `after`
    the last identity read until `done`. `stall` holds while the queue catches up after
    the worker stopped past its TTL: one fresh scan runs meanwhile, not one per pass."""

    after: tuple[str, ...] | None = None
    done: bool = False
    stall: bool = False

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Backfill:
        after = raw.get("after")
        return cls(tuple(after) if after else None, bool(raw.get("done")), bool(raw.get("stall")))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"after": list(self.after)} if self.after else {}
        return out | ({"done": True} if self.done else {}) | ({"stall": True} if self.stall else {})

    def restarted(self) -> Backfill:
        """A fresh scan, still stalled if this one is."""
        return Backfill(stall=self.stall)

    def read_to(self, nxt: tuple[str, ...] | None) -> Backfill:
        """Past one more page: `nxt` is the next page's start, None at the end."""
        return Backfill(after=nxt, done=nxt is None, stall=self.stall)


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
    stored = backfill = Backfill.from_json(await backfill_cursor.read())
    if queue is None:
        # first pass: the queue starts at its tail; everything before it is backfilled
        queue, backfill = list(await store.queue_tail() or []), Backfill()
    behind = bool(queue) and int(queue[0]) < await store.queue_horizon()
    if behind and not backfill.stall:
        # stopped longer than the queue's TTL: writes may have aged out, before or after
        # where a scan in progress is; a fresh scan, once while the queue catches up
        backfill = Backfill(stall=True)
    elif not behind and backfill.stall:
        backfill = replace(backfill, stall=False)  # caught up
    from server.code.domain.index.repos import RegistryUnread

    try:
        configured = code.configured_code_namespaces()
    except RegistryUnread:
        configured = []  # not read yet: nothing links this pass; the sweep below runs
    if {tuple(r) for r in configured} - {tuple(r) for r in await repos_cursor.read()}:
        # a repository configured since: memories read before it now resolve
        backfill = backfill.restarted()
    if backfill != stored:
        # owed before the queue or the repositories are acknowledged: a pass that fails
        # after either still owes it
        await backfill_cursor.write(backfill.to_json())
    if configured:
        # with none nothing links, and the cursor keeps the last ones: their return after
        # a pass that listed none (the registry unread) must not restart the backfill
        await repos_cursor.write(configured)
        position = tuple(queue) or None
        for _ in range(_QUEUE_PAGES):
            read = result.queued
            position = await code_links.link_queued(position, batch, result)
            await queue_cursor.write(list(position or []))
            if result.queued - read < batch:
                break
        if not backfill.done:
            backfill = backfill.read_to(await code_links.backfill(backfill.after, batch, result))
        await backfill_cursor.write(backfill.to_json())
    swept = await code_links.sweep_erased(await sweep_cursor.read(), batch, result)
    await sweep_cursor.write(swept)


__all__ = ["run_pass"]
