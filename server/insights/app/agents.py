"""agents.* handlers — visibility rollups over agent_events."""

from __future__ import annotations

import time
from typing import Any

from server.core.ch import get_client
from server.insights.api.models_agents import (
    AgentsConnectedInput,
    AgentsConnectedOutput,
    ConnectedAgent,
)
from server.insights.store import agents as agents_store

# The sidebar badge asks agents.connected every 30 s per tab, and the answer is a 24 h scan
# of agent_events. One answer per (org,
# namespace, window, caller scope) serves every asker for 60 s — "a new agent in the last
# day" does not change faster than that.
_CONNECTED_TTL_S = 60.0
_connected_cache: dict[tuple[str, str, int, str], tuple[float, AgentsConnectedOutput]] = {}
# How far back `first_seen` looks. Partitioned by month, so a month is one or two partitions
# instead of the whole table.
_FIRST_SEEN_LOOKBACK_DAYS = 30


def reset_connected_cache() -> None:
    """Tests."""
    _connected_cache.clear()


async def handle_agents_connected(input: AgentsConnectedInput) -> AgentsConnectedOutput:
    """Roll up agent_events by agent over a window (last-seen / ops / cost).

    Sentinel agents ("-", "*", "") are filtered server-side. namespace=None
    means org-wide, where non-admin callers are scoped to their accessible
    namespaces — without it a user could see clients writing to other
    namespaces (privacy leak).
    """

    scope_where = ""
    scope_params: dict[str, Any] = {}
    if not input.namespace:
        from server.auth.context import resolve_org_scope

        scope = await resolve_org_scope(input.org)
        if not scope.accessible:
            return AgentsConnectedOutput(rows=[], window_hours=int(input.window_hours))
        scope_where, scope_params = scope.where, dict(scope.params)
    where, params = agents_store.connected_where(
        input.org,
        int(input.window_hours),
        namespace=input.namespace,
        scope_where=scope_where,
        scope_params=scope_params,
    )

    count_only = bool(input.count_only)
    # The key carries the whole predicate, not just its params: two callers whose
    # params are equal can still hold opposite filters (IN vs NOT IN).
    cache_key = (
        input.org,
        input.namespace or "",
        int(input.window_hours),
        where + "|" + repr(sorted(params.items())) + ("|count" if count_only else ""),
    )
    hit = _connected_cache.get(cache_key)
    if hit is not None and time.monotonic() - hit[0] < _CONNECTED_TTL_S:
        return hit[1]
    ch = get_client()
    if count_only:
        n = await agents_store.new_agent_count(
            where, params, lookback_days=_FIRST_SEEN_LOOKBACK_DAYS, ch=ch
        )
        out = AgentsConnectedOutput(rows=[], window_hours=int(input.window_hours), new_count=n)
        _connected_cache[cache_key] = (time.monotonic(), out)
        return out
    # `first_seen` tells a recently-active agent from a newly-appeared one. The badge asks
    # "new in the last day", and an agent idle for the whole lookback and back reads as new
    # again, which is the honest answer for a badge.
    rows = await agents_store.connected_rows(
        where, params, lookback_days=_FIRST_SEEN_LOOKBACK_DAYS, ch=ch
    )
    out = AgentsConnectedOutput(
        rows=[
            ConnectedAgent(
                agent=str(r["agent"]),
                last_seen=r["last_seen"],
                first_seen=r.get("first_seen") or r["last_seen"],
                ops=int(r["ops"]),
                namespaces_touched=int(r["namespaces_touched"]),
                primary_namespace=str(r["primary_namespace"]),
                cost_usd=float(r["cost_usd"] or 0.0),
            )
            for r in rows
        ],
        window_hours=int(input.window_hours),
    )
    if len(_connected_cache) > 512:
        _connected_cache.clear()
    _connected_cache[cache_key] = (time.monotonic(), out)
    return out
