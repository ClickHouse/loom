"""The statements behind memory.proposals: the scan of a namespace's proposed rows, and
what a listed proposal shows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient
from server.identity.domain.read_scope import ReadFilters, scope_where


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
    where = ["org = {org:String}", "namespace = {ns:String}", "tombstoned_at IS NULL"]
    params: dict[str, Any] = {"org": org, "ns": namespace, "cap": cap}
    proposed = ["status = 'proposed'"]
    if prefix:
        proposed.append("startsWith(memory_key, {p:String})")
        params["p"] = prefix
    if tags:
        proposed.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = list(tags)
    # The head is picked across the namespace, as the decision picks it; the read scope
    # then decides which heads this caller may see.
    readable = scope_where(scope, params)
    namespace_rows = " AND ".join(where)
    return await ch.query_all(
        f"""
        SELECT memory_key, mid, version FROM (
            SELECT memory_key, toString(memory_id) AS mid, version, status, tags,
                   user_id, session_id
            FROM loom.memory FINAL
            WHERE {namespace_rows}
              AND memory_key IN (
                  SELECT memory_key FROM loom.memory FINAL
                  WHERE {" AND ".join([*where, *readable, *proposed])}
              )
            ORDER BY memory_key, version DESC, created_at DESC
            LIMIT 1 BY memory_key
        )
        WHERE {" AND ".join([*proposed, *readable])}
        ORDER BY version DESC
        LIMIT {{cap:UInt32}}
        """,
        params=params,
    )


async def proposal_details(
    ch: _CHClient, org: str, namespace: str, located: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """What a listed proposal shows, by memory id, read by the scan's (memory_id, version)
    pairs without FINAL."""
    if not located:
        return {}
    rows = await ch.query_all(
        """
        SELECT toString(memory_id) AS mid, agent, memory_type,
               -- a high value is readable by key only
               if(sensitivity = 'high', '', substringUTF8(value_preview, 1, 400)) AS preview,
               sensitivity, tags, toString(created_at) AS created, conflict_with
        FROM loom.memory
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND (memory_id, version) IN {pairs:Array(Tuple(UUID, UInt64))}
        LIMIT 1 BY memory_id, version
        """,
        params={
            "org": org,
            "ns": namespace,
            "pairs": [(str(r["mid"]), int(r["version"])) for r in located],
        },
    )
    return {str(r["mid"]): r for r in rows}
