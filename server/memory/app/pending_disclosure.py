"""`pending_extraction` for memory.search / memory.ask.

A reader that finds nothing should be able to tell "not saved" from "saved,
not yet extracted". This counts the deferred set_from_messages batches still
pending or running in the searched scope. One indexed count per call,
cached for a few seconds per scope so a burst of searches pays it once;
fail-quiet to 0 (a ClickHouse blip must
never fail a search).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)

_cache: dict[tuple[str, str, str, str], tuple[float, int]] = {}
_MAX_KEYS = 4096


def reset_cache() -> None:
    _cache.clear()


async def pending_extraction(
    *,
    org: str,
    namespace: str,
    agent: str | None,
    user_id: str | None,
    ch: Any | None = None,
) -> int:
    key = (org, namespace, agent or "*", user_id or "-")
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        from server.memory.app.extraction_queue import pending_count

        n = await pending_count(org=org, namespace=namespace, agent=agent, user_id=user_id, ch=ch)
    except Exception as e:  # noqa: BLE001 — a disclosure hint: fails quiet to 0
        log.debug("pending_extraction count failed (fail-quiet): %s", e)
        n = 0
    if len(_cache) >= _MAX_KEYS:
        _cache.clear()
    _cache[key] = (now + settings.memory_pending_disclosure_ttl_s, n)
    return n


__all__ = ["pending_extraction", "reset_cache"]
