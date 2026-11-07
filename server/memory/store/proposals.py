"""The statements behind memory.proposals: the scan of a namespace's proposed rows, and
what a listed proposal shows."""

from __future__ import annotations

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
    tags: list[str] | None = None,
) -> list[Any]:
    """Keys and (memory_id, version) only, newest version first: the order
    memory.proposal_decide picks a key's row by, so a key's first row here is the one a
    decision acts on."""
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
        "status = 'proposed'",
    ]
    params: dict[str, Any] = {"org": org, "ns": namespace, "cap": cap}
    if prefix:
        where.append("startsWith(memory_key, {p:String})")
        params["p"] = prefix
    if tags:
        where.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = list(tags)
    where.extend(scope_where(scope, params))
    rows: list[Any] = await ch.query_all(
        f"""
        SELECT memory_key, toString(memory_id) AS mid, version
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY version DESC, created_at DESC
        LIMIT {{cap:UInt32}}
        """,
        params=params,
    )
    return rows


async def proposal_details(
    ch: _CHClient, org: str, namespace: str, located: list[Any]
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
