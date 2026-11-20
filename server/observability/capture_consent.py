"""Per-namespace consent for capturing prompt/completion text.

`langfuse_capture_io_level` is one server-wide default, which cannot express
per-person consent: it says nothing about whether *Alice* agreed. In loom a
namespace is the per-human unit (`personal-alice`), so the answer belongs there.

Resolution order, most specific first:

    namespace.capture_generation_io = false  -> "off"   (no leg, ever)
    namespace.capture_generation_io = true   -> "all"   (every leg, incl. content)
    namespace.capture_generation_io = NULL   -> settings.langfuse_capture_io_level

Two design constraints drive the shape of this module:

* **The LLM path must stay query-free.** `record_content` runs inside every
  provider call. Doing what `get_namespace_rerank_config` does — a ClickHouse
  query per use — would add a round trip per generation. So the flag is resolved
  once per request and carried on the identity ContextVar; this module only
  answers from a TTL cache.
* **Fail CLOSED.** Every other observability helper in this package fails open,
  because losing telemetry is better than failing a request. This one is the
  exception: if we cannot determine consent, we do not capture. A privacy control
  that fails open is not a privacy control.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)

# One entry per namespace, refreshed on read. 60s means a user toggling the switch
# in the UI sees it take effect within a minute — stated in the UI copy so the lag
# is not a surprise. Shorter would put a query on the hot path for no real gain;
# longer starts to feel like the setting is ignored.
_TTL_S = 60.0
# Bounded so a hostile or buggy caller cannot grow this without limit. On overflow
# the whole cache is dropped rather than evicting one entry: a miss costs one query
# and re-populates, and the alternative is tracking LRU order for a map that is one
# row per tenant in practice.
_MAX_ENTRIES = 5000

# (org, namespace) -> (expires_at_monotonic, consent | None)
_cache: dict[tuple[str, str], tuple[float, bool | None]] = {}


def global_default() -> str:
    """The operator's level for namespaces that have not decided."""
    level = str(settings.langfuse_capture_io_level)
    # Fail closed on an unrecognised or empty value rather than assuming the permissive end.
    return level if level in ("off", "query", "all") else "off"


def _level_for(override: bool | None) -> str:
    """A namespace's stored answer as a capture level."""
    if override is None:
        return global_default()
    return "all" if override else "off"


async def resolve(org: str, namespace: str) -> str:
    """The capture level for this namespace: `"off"`, `"query"` or `"all"`.

    Async and TTL-cached: call once per request from the transport, then carry the
    answer on the identity ContextVar. Do NOT call this per LLM call.
    """
    if not org or not namespace:
        return global_default()

    key = (org, namespace)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return _level_for(hit[1])

    override = await _load(org, namespace)
    if len(_cache) >= _MAX_ENTRIES:
        _cache.clear()
    _cache[key] = (now + _TTL_S, override)
    return _level_for(override)


# Least to most permissive: a read spanning several namespaces captures at the
# first of these any one of them resolves to.
_BY_RESTRICTION = ("off", "query", "all")


async def resolve_most_restrictive(org: str, namespaces: list[str]) -> str:
    """The capture level for a read that spans `namespaces`.

    One call's prompts can carry text from every namespace it searched, so the
    namespace that consented least decides: any "off" makes the whole call "off".
    An empty list resolves like an unnamed namespace (the operator default).
    """
    import asyncio

    unique = [ns for ns in dict.fromkeys(namespaces) if ns]
    if not unique:
        return global_default()
    levels = await asyncio.gather(*(resolve(org, ns) for ns in unique))
    return min(levels, key=_BY_RESTRICTION.index)


async def narrow_to(org: str, namespaces: list[str]) -> None:
    """Lower this request's capture level to the most restrictive of
    `namespaces`' — a fan-out read carries text from every namespace it
    searched. Fails closed: consent that cannot be resolved captures nothing."""
    from server.observability import genai

    try:
        level = await resolve_most_restrictive(org, namespaces)
    except Exception:  # noqa: BLE001 — fail closed, not open
        level = "off"
    genai.narrow_capture_level(level)


async def level_uncached(org: str, namespace: str) -> str:
    """`resolve`'s level read past the cache, raising when the consent cannot be read, so
    a caller that deletes on `off` can tell an unreadable consent from a refusal."""
    return _level_for(await _read(org, namespace))


async def _read(org: str, namespace: str) -> bool | None:
    from server.core.ch import get_client

    row = await get_client().query_one(
        """
        SELECT capture_generation_io
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    if not row:
        # An unregistered namespace has expressed no preference; inherit.
        return None
    value = row.get("capture_generation_io")
    return None if value is None else bool(value)


async def _load(org: str, namespace: str) -> bool | None:
    """The namespace's stored override, or None to inherit.

    A lookup failure returns False — not None — so an unreachable ClickHouse means
    "do not capture" rather than "fall back to the permissive global". This is the
    one place in this package that fails closed, and the reason is that the cost of
    the two failure directions is not symmetric: failing closed loses some
    debugging detail, failing open publishes text a user may have declined.
    """
    try:
        return await _read(org, namespace)
    except Exception as e:  # noqa: BLE001 — fail CLOSED: an unreadable consent reads as off
        log.warning(
            "capture-consent lookup failed for %s/%s (%s); NOT capturing",
            org,
            namespace,
            e,
        )
        # False, not None: an unreachable ClickHouse must resolve to "off", never
        # fall back to the operator's (possibly permissive) level.
        return False


def invalidate(org: str = "", namespace: str = "") -> None:
    """Drop cached consent so the next request re-reads it.

    Called when a namespace's settings are written, so an operator or user who
    toggles the switch does not have to wait out the TTL.
    """
    if org and namespace:
        _cache.pop((org, namespace), None)
    else:
        _cache.clear()


def status() -> dict[str, Any]:
    return {"cached_namespaces": len(_cache), "global_default": global_default()}


__all__ = ["global_default", "invalidate", "resolve", "resolve_most_restrictive", "status"]
