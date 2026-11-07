"""The statements behind memory.proposals: the scan of a namespace's proposed rows, and
what a listed proposal shows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient
from server.identity.domain.read_scope import ReadFilters, scope_and, scope_where

# The characters of a value a listed proposal shows (MemoryProposal.value_preview).
PREVIEW_CHARS = 400


async def scan_proposals(
    ch: _CHClient,
    org: str,
    namespace: str,
    *,
    scope: ReadFilters,
    cap: int,
    prefix: str = "",
    tags: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """The keys whose newest row in the namespace is a proposal `scope` may read, with
    that row's (memory_id, version), newest first. memory.proposal_decide takes a key's
    newest row of any status, so an older proposal under a decided key is left out: no
    decision could take it."""
    params: dict[str, Any] = {"org": org, "ns": namespace, "cap": cap}
    live_in_namespace = "org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL"
    # A wanted row: a proposal this caller may read, matching the listing's filters.
    filters = ["status = 'proposed'", *scope_where(scope, params)]
    if prefix:
        filters.append("startsWith(memory_key, {p:String})")
        params["p"] = prefix
    if tags:
        filters.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = list(tags)
    is_wanted = " AND ".join(filters)
    return await ch.query_all(
        f"""
        SELECT memory_key, mid, version FROM (
            -- the newest row's id and the columns `is_wanted` reads
            SELECT memory_key, toString(memory_id) AS mid, version, status, tags,
                   user_id, session_id
            FROM loom.memory FINAL
            WHERE {live_in_namespace}
              AND memory_key IN (
                  SELECT memory_key FROM loom.memory FINAL WHERE {live_in_namespace} AND {is_wanted}
              )
            ORDER BY memory_key, version DESC, created_at DESC
            LIMIT 1 BY memory_key
        )
        WHERE {is_wanted}
        ORDER BY version DESC
        LIMIT {{cap:UInt32}}
        """,
        params=params,
    )


async def proposal_details(
    ch: _CHClient,
    org: str,
    namespace: str,
    located: list[dict[str, Any]],
    *,
    scope: ReadFilters,
) -> dict[str, dict[str, Any]]:
    """What a listed proposal shows, by memory id, read by (memory_id, version) pairs
    without FINAL, within `scope`."""
    if not located:
        return {}
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "chars": PREVIEW_CHARS,
        "pairs": [(str(r["mid"]), int(r["version"])) for r in located],
    }
    within = scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT toString(memory_id) AS mid, agent, user_id, session_id, memory_type,
               -- a high value is readable by key only
               if(sensitivity = 'high', '', substringUTF8(value_preview, 1, {{chars:UInt32}}))
                   AS preview,
               -- value_preview holds 2048 bytes: a longer value is past any 400 characters
               sensitivity != 'high'
                   AND (value_len > 2048 OR lengthUTF8(value_preview) > {{chars:UInt32}}) AS cut,
               sensitivity, tags, toString(created_at) AS created, conflict_with
        FROM loom.memory
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND (memory_id, version) IN {{pairs:Array(Tuple(UUID, UInt64))}}{within}
        LIMIT 1 BY memory_id, version
        """,
        params=params,
    )
    return {str(r["mid"]): r for r in rows}
