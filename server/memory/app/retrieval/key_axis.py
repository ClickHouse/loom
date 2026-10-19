"""The key axis: whether the fact leg also ranks by the memory-key
embedding. A deployment setting, a request override, or an automatic
per-namespace probe of key reuse cached per process here.
"""

from __future__ import annotations

import logging
from typing import Any

from server.memory.domain.search_types import SearchQuery

log = logging.getLogger("server.memory.app.retrieval")


def key_axis_from_reuse(reused_share: float, threshold: float) -> bool:
    """Attribute-shaped keys are re-written (`user_task_worktree` once per
    session); extractor keys on chat are not. At or above the threshold the
    key axis is on."""
    return float(reused_share) >= float(threshold)


# (org, namespace) -> (probed_at_monotonic, reused_share; None when the probe failed).
# The share is cached, not the decision, so a changed threshold applies at once.
_KEY_AXIS_AUTO: dict[tuple[str, str], tuple[float, float | None]] = {}


def _decide(share: float | None, settings_obj: Any) -> bool:
    if share is None:
        return False  # a failed probe reads as off, the cheaper side
    return key_axis_from_reuse(share, float(settings_obj.memory_search_key_axis_auto_reuse_share))


def key_axis_auto_cached(
    org: str, namespace: str, settings_obj: Any | None = None
) -> tuple[bool, float] | None:
    """The last auto decision for a namespace, for the handler's ledger."""
    from server.settings import settings as _default_settings

    hit = _KEY_AXIS_AUTO.get((str(org), str(namespace)))
    if hit is None:
        return None
    _s = _default_settings if settings_obj is None else settings_obj
    return _decide(hit[1], _s), hit[1] or 0.0


async def key_axis_auto(
    org: str, namespace: str, *, ch: Any, settings_obj: Any | None = None
) -> bool:
    """Decide the key axis for one namespace from its key reuse, cached for
    memory_search_key_axis_auto_ttl_seconds. One GROUP BY over the
    namespace's keys; a failed probe reads as off (the cheaper side)."""
    import time as _time

    from server.settings import settings as _default_settings

    _s = _default_settings if settings_obj is None else settings_obj
    ttl = float(_s.memory_search_key_axis_auto_ttl_seconds or 600)
    now = _time.monotonic()
    key = (str(org), str(namespace))
    hit = _KEY_AXIS_AUTO.get(key)
    if hit is not None and now - hit[0] < ttl:
        return _decide(hit[1], _s)
    share = 0.0
    try:
        rows = await ch.query_all(
            """
            SELECT countIf(n > 1) / greatest(count(), 1) AS reused_share
            FROM (
                SELECT memory_key, count() AS n
                FROM loom.memory_embeddings FINAL
                WHERE org = {org:String} AND namespace = {ns:String}
                  AND tombstoned_at IS NULL
                GROUP BY memory_key
            )
            """,
            {"org": str(org), "ns": str(namespace)},
        )
        if rows:
            share = float((rows[0] or {}).get("reused_share") or 0.0)
    except Exception as e:  # noqa: BLE001 — a probe, never a failure; off is the cheaper side
        log.warning("memory.search: key-axis auto probe failed for %s/%s: %s", org, namespace, e)
        _KEY_AXIS_AUTO[key] = (now, None)
        return False
    _KEY_AXIS_AUTO[key] = (now, share)
    return _decide(share, _s)


async def resolve_key_axis(q: SearchQuery, *, ch: Any, settings_obj: Any | None = None) -> bool:
    """The request's `key_axis` when given; else the deployment mode: on,
    off, or auto (per namespace)."""
    if q.key_axis is not None:
        return bool(q.key_axis)
    from server.settings import settings as _default_settings

    _s = _default_settings if settings_obj is None else settings_obj
    mode = _s.memory_search_key_axis
    if mode == "on":
        return True
    if mode == "off":
        return False
    return await key_axis_auto(q.org, q.namespace, ch=ch, settings_obj=_s)
