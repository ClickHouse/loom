"""A write's sensitivity level, read off the event loop when the value is long."""

from __future__ import annotations

import asyncio

from server.memory.domain.sensitivity import Sensitivity, resolve_sensitivity

# Detection reads up to about a second per megabyte (one pass per shape): past this many
# characters of one request's values, it runs on a worker thread, so a long value never
# holds the event loop.
ON_LOOP_CHARS = 64 * 1024


async def resolve_level(requested: str | None, text: str, *, read_before: int = 0) -> Sensitivity:
    """`resolve_sensitivity(requested, text)`, on a worker thread once `read_before` (the
    characters this request's earlier values read) plus `text` pass `ON_LOOP_CHARS`."""
    if read_before + len(text) <= ON_LOOP_CHARS:
        return resolve_sensitivity(requested, text)
    return await asyncio.to_thread(resolve_sensitivity, requested, text)
