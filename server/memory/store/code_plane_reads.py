"""The memory rows the code plane reads: lesson excerpts for a symbol card and
the files a user's memories name. The code plane reaches them through
`server.memory.app.code_plane_reads`, never with its own SQL over memory tables."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def lesson_corpus(
    org: str, namespaces: Sequence[str], *, cap: int, ch: _CHClient | None = None
) -> list[dict[str, Any]]:
    """Up to `cap` live excerpt rows of the lesson namespaces, shortest first,
    with the lower-cased excerpt and the files facet the matcher indexes."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            """
            SELECT memory_key, namespace, content_excerpt, content_excerpt_lc, files
            FROM loom.memory_embeddings FINAL
            WHERE org = {org:String} AND namespace IN {nss:Array(String)}
              AND tombstoned_at IS NULL
            ORDER BY length(content_excerpt) ASC
            LIMIT {cap:UInt32}
            """,
            {"org": org, "nss": list(namespaces), "cap": int(cap)},
        )
    )


async def lessons_matching(
    org: str,
    namespaces: Sequence[str],
    *,
    token: str,
    paths: Sequence[str],
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Live excerpt rows of the lesson namespaces whose excerpt holds `token` or
    whose files facet names one of `paths`, shortest first."""
    ch = ch or _default_client()
    return list(
        await ch.query_all(
            """
            SELECT memory_key, namespace, content_excerpt, files
            FROM loom.memory_embeddings FINAL
            WHERE org = {org:String} AND namespace IN {nss:Array(String)}
              AND tombstoned_at IS NULL
              AND (hasToken(content_excerpt_lc, {tok:String})
                   OR hasAny(files, {paths:Array(String)}))
            ORDER BY length(content_excerpt) ASC
            LIMIT {limit:UInt32}
            """,
            {
                "org": org,
                "nss": list(namespaces),
                "tok": token,
                "paths": list(paths),
                "limit": int(limit),
            },
        )
    )


async def user_file_paths(
    org: str, user_id: str, *, limit: int = 200, ch: _CHClient | None = None
) -> list[str]:
    """The distinct files the user's live memories name, sorted, at most `limit`."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT DISTINCT arrayJoin(files) AS f FROM loom.memory FINAL
        WHERE org = {org:String}
          AND user_id = {uid:String} AND notEmpty(files)
          AND tombstoned_at IS NULL
        ORDER BY f LIMIT {lim:UInt32}
        """,
        {"org": org, "uid": user_id, "lim": int(limit)},
    )
    return [str(r["f"]) for r in rows]
