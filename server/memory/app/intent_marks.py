"""Resolve the intents (owed follows, undeclared-edge drops) whose work landed, a batch
at a time: a request waits on nothing, and a flush is one chunked mark for every org
it holds instead of a mutation per write. Process-local: an intent this process did
not flush (a crash, a failed mark) stays active, and the projection repair resolves
it."""

from __future__ import annotations

import asyncio
import logging

from server.core.background import detached_task
from server.core.ch import set_query_tag

log = logging.getLogger(__name__)

# Seconds between flushes.
FLUSH_S = 10.0

# org -> intent ids queued for the next flush. The ids came from this process's own
# writes, so one mark across their orgs reaches no other tenant's rows.
_pending: dict[str, set[str]] = {}
_flusher: asyncio.Task[None] | None = None


def resolve_soon(org: str, intents: list[str]) -> None:
    """Queue `intents` of `org` for the next flush (called from a running loop)."""
    global _flusher
    if not intents:
        return
    _pending.setdefault(org, set()).update(intents)
    if _flusher is None or _flusher.done():
        _flusher = detached_task(_flush_every(), name="intent-marks")


async def flush() -> None:
    """Mark every queued intent resolved, in one chunked mark across their orgs."""
    from server.memory.app.projection_failures import mark_resolved

    batch = {org: set(ids) for org, ids in _pending.items()}
    _pending.clear()
    ids = sorted(i for org_ids in batch.values() for i in org_ids)
    if not ids:
        return
    try:
        await mark_resolved(sorted(batch), ids)
    except asyncio.CancelledError:
        # Stopped mid-mark (a shutdown): the next flush, or the drain, marks them.
        for org, org_ids in batch.items():
            _pending.setdefault(org, set()).update(org_ids)
        raise
    except Exception as e:  # noqa: BLE001 — left active, the repair resolves them
        log.warning("intent marks: %d intents left for the repair: %s", len(ids), e)


async def _flush_every() -> None:
    set_query_tag("intent_marks")
    while _pending:
        await asyncio.sleep(FLUSH_S)
        await flush()


async def drain() -> None:
    """Flush what is queued now (shutdown)."""
    global _flusher
    if _flusher is not None and not _flusher.done():
        _flusher.cancel()
        try:
            await _flusher
        except asyncio.CancelledError:
            pass
    _flusher = None
    await flush()
