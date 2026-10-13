"""Code link pass: link the memories written since the last pass to the code they name
(and, on the first pass or after falling behind the queue, the ones written before),
then follow every repository's links to its indexed head and run the memories' `revise`
policy (`server.memory.app.code_links`)."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from server.core.errors import PreconditionFailedError
from server.workers.status import log_pass_errors, record_pass

if TYPE_CHECKING:
    from server.memory.app.code_links import PassResult

log = logging.getLogger(__name__)

_DONE = "done"
# queue pages read a pass, each `memory_code_links_batch` long, while they come full
_QUEUE_PAGES = 10


async def run_pass() -> dict[str, Any]:
    from server.code.app import memory_links as code
    from server.memory.app import code_links
    from server.settings import settings

    batch = int(settings.memory_code_links_batch)
    result = code_links.PassResult()
    with record_pass("memory_code_links") as rec:
        await code.refresh_repos()
        try:
            # one host reads the queue at a time: two would link the same writes twice
            async with code_links.queue_lease():
                await _link_writes(batch, result)
        except PreconditionFailedError:
            pass  # another host is linking them
        await code_links.recheck_all(result)
        rec.outcome = result.as_dict()
        log_pass_errors(log, "memory_code_links", result.errors)
    return result.as_dict()


async def _link_writes(batch: int, result: PassResult) -> None:
    """Link the queued writes, the backfill's next page, and sweep erased memories."""
    from server.code.app import memory_links as code
    from server.memory.app import code_links
    from server.memory.store import code_links as store

    raw = await code.link_cursor("queue")
    backfill = await code.link_cursor("backfill")
    if not raw:
        # first pass: the queue starts at its tail; everything before it is backfilled
        tail = await store.queue_tail()
        raw, backfill = json.dumps(list(tail) if tail else []), ""
    queue = tuple(json.loads(raw)) or None
    if queue and int(queue[0]) < await store.queue_horizon() and backfill == _DONE:
        backfill = ""  # stopped longer than the queue's TTL: writes may have aged out
    configured = code.configured_code_namespaces()
    seen = {tuple(r) for r in json.loads(await code.link_cursor("repos") or "[]")}
    if {tuple(r) for r in configured} - seen:
        # a repository configured since: memories read before it now resolve. The backfill
        # is recorded before the repository is, so a pass that fails here still owes it
        backfill = ""
        await code.set_link_cursor("backfill", backfill)
    await code.set_link_cursor("repos", json.dumps(configured))
    for _ in range(_QUEUE_PAGES):
        read = result.queued
        queue = await code_links.link_queued(queue, batch, result)
        await code.set_link_cursor("queue", json.dumps(list(queue or [])))
        if result.queued - read < batch:
            break
    if backfill != _DONE:
        nxt = await code_links.backfill(tuple(json.loads(backfill or "[]")) or None, batch, result)
        backfill = _DONE if nxt is None else json.dumps(list(nxt))
    await code.set_link_cursor("backfill", backfill)
    swept = await code_links.sweep_erased(await code.link_cursor("sweep"), batch, result)
    await code.set_link_cursor("sweep", swept)


__all__ = ["run_pass"]
