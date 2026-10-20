"""Per-namespace switch for the extraction coverage sweep.

The coverage sweep reads the turns no fact cited once more. It pays on
dense fact corpora and buys nothing on chatty conversation, and the corpus is
a property of the namespace, so the switch lives on `loom.namespaces`:

    namespace.coverage_sweep = true   -> sweep every ingest
    namespace.coverage_sweep = false  -> never sweep
    namespace.coverage_sweep = NULL   -> the ingest's own shape (a statement-dense turn)

Resolved once per ingest (not per segment) and TTL-cached like the capture
consent; fails OPEN to "inherit" — a lookup failure must not silently change
what an ingest extracts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)

_MAX_ENTRIES = 5000
_cache: dict[tuple[str, str], tuple[float, bool | None]] = {}


def effective(
    override: bool | None, settings: Any, messages: list[dict[str, Any]] | None = None
) -> bool:
    """The switch an ingest runs with: the namespace's (or the caller's)
    answer, else the ingest's own shape — a statement-dense turn turns the
    sweep on for that ingest. An explicit `false` still wins over density."""
    if override is not None:
        return bool(override)
    return density_fires(messages, settings)


def density_fires(messages: list[dict[str, Any]] | None, settings: Any) -> bool:
    """The ingest holds a turn with at least
    `memory_extract_coverage_sweep_auto_min_statements` statements."""
    if not messages:
        return False
    n = int(settings.memory_extract_coverage_sweep_auto_min_statements)
    if n <= 0:
        return False
    from server.memory.app.extract import statement_dense_turns

    return statement_dense_turns(messages, min_statements=n) > 0


async def resolve(org: str, namespace: str) -> bool | None:
    """The namespace's stored override, or None to inherit. TTL-cached."""
    if not org or not namespace:
        return None
    key = (org, namespace)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    override = await _load(org, namespace)
    if len(_cache) >= _MAX_ENTRIES:
        _cache.clear()
    _cache[key] = (now + settings.memory_coverage_sweep_gate_ttl_s, override)
    return override


async def _load(org: str, namespace: str) -> bool | None:
    try:
        from server.core.ch import get_client

        row = await get_client().query_one(
            """
            SELECT coverage_sweep
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    except Exception as e:  # noqa: BLE001 — fail open to the density gate
        log.warning("coverage-sweep lookup failed for %s/%s (%s); inheriting", org, namespace, e)
        return None
    if not row:
        return None
    value = row.get("coverage_sweep")
    return None if value is None else bool(value)


def invalidate(org: str = "", namespace: str = "") -> None:
    """Drop the cached answer so a namespace write applies on the next ingest."""
    if org and namespace:
        _cache.pop((org, namespace), None)
    else:
        _cache.clear()
