"""The namespace registry's answer to "what class is this traffic?".

Every live namespace has a `loom.namespaces` row with a `kind`; the onboarding kit puts
`traffic_class:<value>` in the row's tags. This module reads that row once per (org,
namespace) per process, keeps the class for `traffic_class_registry_ttl_seconds`, and offers
two views of the cache:

* `cached_class(org, namespace)` — synchronous, cache only. The HTTP
  transport's rate-limit check runs BEFORE authorization (an anonymous
  request must 401 without touching a tenant's bucket), so it may not read
  ClickHouse; it takes what the cache holds, and the request's hint
  otherwise. From the second request on the class is the row's.
* `registry_class(org, namespace)` — the point read, after authorization,
  filling the cache. Best-effort: a registry failure yields '' (no row) and
  is retried on the next request. Never raises.

A miss (no row) is cached too, for the same TTL, so a namespace nobody
registered costs one read per TTL, not one per request.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Final

from server.observability.traffic_class import BENCH, class_from_registry_row
from server.settings import settings

log = logging.getLogger("loom.traffic_class")

_cache: Final[dict[tuple[str, str], tuple[str, float]]] = {}
_MAX_CACHE = 200_000
_MISSING = ("", "-", "*")

REGISTRY_CLASS_SQL = """
SELECT kind, tags
FROM loom.namespaces FINAL
WHERE org = {org:String} AND namespace = {ns:String}
LIMIT 1
"""


def _ttl() -> float:
    return max(1.0, float(settings.traffic_class_registry_ttl_seconds))


def _remember(org: str, namespace: str, cls: str, *, now: float | None = None) -> None:
    if len(_cache) >= _MAX_CACHE:
        _cache.clear()
    _cache[(org, namespace)] = (cls, (time.monotonic() if now is None else now) + _ttl())


def cached_class(org: str | None, namespace: str | None, *, now: float | None = None) -> str | None:
    """The cached class ('' = a row was looked up and there is none); None
    when the cache holds nothing fresh for the pair."""
    if not org or not namespace:
        return None
    hit = _cache.get((org, namespace))
    if hit is None:
        return None
    cls, expires = hit
    if (time.monotonic() if now is None else now) >= expires:
        _cache.pop((org, namespace), None)
        return None
    return cls


def forget_all() -> None:
    """Test seam: drop the process cache."""
    _cache.clear()


async def registry_class(org: str | None, namespace: str | None, *, ch: Any | None = None) -> str:
    """The class the registry row implies (see
    `traffic_class.class_from_registry_row`); '' when there is no row or the
    read failed. Cached per (org, namespace) for the TTL."""
    if not org or not namespace or org in _MISSING or namespace in _MISSING:
        return ""
    hit = cached_class(org, namespace)
    if hit is not None:
        return hit
    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        row = await ch.query_one(REGISTRY_CLASS_SQL, params={"org": org, "ns": namespace})
        cls = class_from_registry_row(row)
        _remember(org, namespace, cls)
        return cls
    except Exception as e:  # noqa: BLE001 — a class lookup never fails a request
        log.debug("traffic class registry read failed for %s/%s: %s", org, namespace, e)
        return ""


def note_registered(org: str, namespace: str, row: dict[str, Any]) -> None:
    """A registration this process just wrote: cache its class so the next
    request does not re-read a row we have in hand."""
    if org and namespace:
        _remember(org, namespace, class_from_registry_row(row))


def namespace_prefix_hint(namespace: str | None) -> str:
    """`bench` when the namespace is under one of the registry's bench
    prefixes — the kind the first-write hook would register it as — so the
    very first request into a fresh bench namespace is classed before its
    row exists. '' otherwise."""
    if not namespace:
        return ""
    try:
        from server.identity.app.namespace_autoregister import classify_kind

        return BENCH if classify_kind(namespace) == BENCH else ""
    except Exception:  # noqa: BLE001 — a hint, never a failure
        return ""


def classify_cached(
    auth: Any, org: str | None, namespace: str | None, raw_args: dict[str, Any] | None
) -> tuple[str, str]:
    """The request's class from what is known WITHOUT a read: the token claim,
    the cached registry class, the request's hint (its field / tag /
    consumer, or a bench-prefixed namespace). For the rate-limit check that
    runs before authorization."""
    from server.observability.traffic_class import request_hint, resolve_traffic_class

    claim = str(getattr(auth, "traffic_class", "") or "") if auth is not None else ""
    return resolve_traffic_class(
        claim=claim,
        registry_class=cached_class(org, namespace),
        hint=request_hint(raw_args) or namespace_prefix_hint(namespace),
    )


async def classify(
    auth: Any, org: str | None, namespace: str | None, raw_args: dict[str, Any] | None
) -> tuple[str, str]:
    """The request's class, reading the registry row when the claim does not
    settle it and the cache holds nothing. For after authorization."""
    from server.observability.traffic_class import (
        SOURCE_CLAIM,
        request_hint,
        resolve_traffic_class,
    )

    cls, source = classify_cached(auth, org, namespace, raw_args)
    if source == SOURCE_CLAIM or cached_class(org, namespace) is not None:
        return cls, source
    reg = await registry_class(org, namespace)
    return resolve_traffic_class(
        claim="",
        registry_class=reg,
        hint=request_hint(raw_args) or namespace_prefix_hint(namespace),
    )


__all__ = [
    "REGISTRY_CLASS_SQL",
    "cached_class",
    "classify",
    "classify_cached",
    "forget_all",
    "namespace_prefix_hint",
    "note_registered",
    "registry_class",
]
