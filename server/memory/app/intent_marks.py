"""Resolve the FOLLOW_OWED intents of point-write retirements whose record and follow both
landed, a batch at a time: a request waits on nothing, and a flush is one chunked mark
per org instead of a mutation per write. Process-local: an intent this process did not
flush (a crash, a failed mark) stays active, and the projection repair resolves it."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

# Seconds between flushes.
FLUSH_S = 10.0

_pending: dict[str, set[str]] = {}
_flusher: asyncio.Task[None] | None = None


def resolve_soon(org: str, intents: list[str]) -> None:
    """Queue `intents` of `org` for the next flush."""
    global _flusher
    if not intents:
        return
    _pending.setdefault(org, set()).update(intents)
    if _flusher is None or _flusher.done():
        try:
            _flusher = asyncio.get_running_loop().create_task(_flush_every(), name="intent-marks")
        except RuntimeError:  # no loop: the repair resolves them
            _pending.clear()


async def flush() -> None:
    """Mark every queued intent resolved, one chunked mark per org."""
    from server.memory.app.projection_failures import mark_resolved

    batch = {org: sorted(ids) for org, ids in _pending.items()}
    _pending.clear()
    for org, ids in batch.items():
        try:
            await mark_resolved(org, ids)
        except Exception as e:  # noqa: BLE001 — left active, the repair resolves them
            log.warning("intent marks: %d intents left for the repair: %s", len(ids), e)


async def _flush_every() -> None:
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
