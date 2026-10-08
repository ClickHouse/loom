"""Namespace-level retrieval defaults for memory.search.

A namespace row carries `retrieval_defaults`: a JSON object naming what an
unpinned search runs — `search_mode`, `top_k`, `auto_touch`, `include_tags`,
`bodies`, `reinforcement_weight`, each optional. memory.search applies each
one to a field the caller did not set, after a recipe (a caller choice) and
before the deployment default (`memory_search_default_mode`).
A namespace-defaulted mode is still a *default* to the planner: the planner's
shape rules (an identifier in the query takes the lexical fusion) run on it
exactly as they run on the deployment's.

The row is read once per (org, namespace) per TTL and cached; a lookup
failure (ClickHouse away, say) resolves to "no defaults", so a search never
changes shape because a directory read failed.
`namespace.register` refreshes the cache from the row it just wrote, so a
change applies to the next request in this process without a re-read.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# The knobs a namespace may default, in the order the plan reports them.
FIELDS: tuple[str, ...] = (
    "search_mode",
    "top_k",
    "auto_touch",
    "include_tags",
    "bodies",
    "reinforcement_weight",
    "recency_pool_weight",
    "min_similarity",
    "floor_mode",
    "chunk_max_share",
)

_TTL_S = 60.0
_MAX_ENTRIES = 5000
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def parse(raw: Any) -> dict[str, Any]:
    """The stored column as a validated dict of set knobs; {} for '' or for
    anything the model rejects (a row written by hand with a bad value must
    not take a search down with it — it is logged and ignored)."""
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("namespace retrieval_defaults is not JSON; ignored: %r", text[:80])
        return {}
    if not isinstance(data, dict):
        return {}
    # A stored row is read by the knobs this build has: a key it does not know
    # is dropped, so the row's other defaults keep applying.
    unknown = sorted(k for k in data if k not in FIELDS)
    if unknown:
        log.warning("namespace retrieval_defaults keys not read: %s", unknown)
    data = {k: v for k, v in data.items() if k in FIELDS}
    from server.identity.api.models import NamespaceRetrievalDefaults

    try:
        model = NamespaceRetrievalDefaults.model_validate(data)
    except Exception as exc:  # noqa: BLE001 — a bad stored value is a warning, not an outage
        log.warning("namespace retrieval_defaults rejected; ignored: %s", str(exc)[:160])
        return {}
    return as_dict(model)


def as_dict(model: Any) -> dict[str, Any]:
    """Only the knobs the model set (None = unset), in FIELDS order."""
    out: dict[str, Any] = {}
    for name in FIELDS:
        value = getattr(model, name, None)
        if value is not None:
            out[name] = value
    return out


def serialize(model: Any) -> str:
    """The column value for a validated model: compact, key-sorted JSON of
    the set knobs; '' when none is set (the row reads as "no defaults")."""
    if isinstance(model, dict):
        data = {k: v for k, v in model.items() if v is not None}
    else:
        data = as_dict(model)
    if not data:
        return ""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def note_registered(org: str, namespace: str, row: dict[str, Any]) -> None:
    """Cache the defaults of the row `namespace.register` just wrote."""
    _put(org, namespace, parse(row.get("retrieval_defaults")))


def invalidate(org: str, namespace: str) -> None:
    _cache.pop((org, namespace), None)


def clear_cache() -> None:
    _cache.clear()


def _put(org: str, namespace: str, defaults: dict[str, Any]) -> None:
    if len(_cache) >= _MAX_ENTRIES:
        _cache.clear()
    _cache[(org, namespace)] = (time.monotonic() + _TTL_S, defaults)


async def resolve(org: str, namespace: str, ch: Any | None = None) -> dict[str, Any]:
    """The namespace's set knobs, {} when none. TTL-cached; fails open to {}."""
    if not org or not namespace:
        return {}
    key = (org, namespace)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit is not None and hit[0] > now:
        return hit[1]
    defaults: dict[str, Any] = {}
    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        row = await ch.query_one(
            """
            SELECT retrieval_defaults
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
        defaults = parse((row or {}).get("retrieval_defaults"))
    except Exception as exc:  # noqa: BLE001 — a directory read must never change a search's shape
        log.warning("namespace retrieval_defaults lookup failed (%s); none applied", exc)
        defaults = {}
    _put(org, namespace, defaults)
    return defaults


def apply(input: Any, defaults: dict[str, Any]) -> Any:
    """Fill every knob in `defaults` the caller left unset.

    A field in `model_fields_set` is the caller's (an explicit value equal
    to the wire default still counts); a recipe owns `search_mode`. The
    names applied ride the private attr `_namespace_defaults_applied`, and a
    namespace-defaulted mode marks `_search_mode_defaulted` so the planner
    treats it as it treats the deployment's. Returns the same
    object when nothing applies, so a namespace without defaults costs no
    copy.
    """
    if not defaults:
        return input
    was_set = set(getattr(input, "model_fields_set", set()) or set())
    recipe = (getattr(input, "recipe", "") or "").strip()
    updates: dict[str, Any] = {}
    for name in FIELDS:
        if name not in defaults or name in was_set:
            continue
        if name == "search_mode" and recipe:
            continue
        updates[name] = defaults[name]
    if not updates:
        return input
    applied = tuple(updates)
    if "search_mode" in updates:
        updates["_search_mode_defaulted"] = True
    updates["_namespace_defaults_applied"] = applied
    return input.model_copy(update=updates)
