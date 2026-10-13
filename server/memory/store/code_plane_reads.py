"""The memory rows the code plane reads: the memories linked to a symbol card and
the files a user's memories name. The code plane reaches them through
`server.memory.app.code_plane_reads`, never with its own SQL over memory tables."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient, _default_client, query_all_sliced
from server.identity.domain.read_scope import ReadFilters, scope_and


async def served_excerpts(
    org: str, memory_ids: Sequence[str], *, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """The served rows among `memory_ids` (live, unexpired, not `high`), with the first
    400 characters of the value as text (a JSON string value decoded), for a code read's
    lessons."""
    if not memory_ids:
        return []
    return await query_all_sliced(
        ch or _default_client(),
        """
        SELECT toString(memory_id) AS mid, memory_key, namespace,
               substringUTF8(
                   if(startsWith(value, '"'), JSONExtractString(value), value), 1, 400
               ) AS excerpt, pinned, confidence
        FROM loom.memory FINAL
        WHERE org = {org:String} AND memory_id IN {ids:Array(UUID)}
          AND tombstoned_at IS NULL AND status IN ('active', 'conflicted')
          AND (expires_at IS NULL OR expires_at > now()) AND sensitivity != 'high'
        """,
        {"org": org, "ids": sorted({str(i) for i in memory_ids})},
        "ids",
    )


async def user_file_paths(
    org: str,
    user_id: str,
    namespaces: Sequence[str] | None,
    *,
    scope: ReadFilters,
    limit: int = 200,
    ch: _CHClient | None = None,
) -> list[str]:
    """The distinct files the user's live memories in `namespaces` and inside `scope`
    name, sorted, at most `limit`. `namespaces=None` reads every namespace of the org
    (no caller to bound it)."""
    if namespaces is not None and not namespaces:
        return []
    ch = ch or _default_client()
    params: dict[str, Any] = {"org": org, "uid": user_id, "lim": int(limit)}
    in_namespaces = ""
    if namespaces is not None:
        in_namespaces = "AND namespace IN {nss:Array(String)}"
        params["nss"] = list(namespaces)
    in_scope = scope_and(scope, params)
    rows = await ch.query_all(
        f"""
        SELECT DISTINCT arrayJoin(files) AS f FROM loom.memory FINAL
        WHERE org = {{org:String}} {in_namespaces}
          AND user_id = {{uid:String}} AND notEmpty(files)
          AND tombstoned_at IS NULL{in_scope}
        ORDER BY f LIMIT {{lim:UInt32}}
        """,
        params,
    )
    return [str(r["f"]) for r in rows]
