"""Recency seeds for the graph walk.

The graph walk's breadth-first search also starts from the most recent
episodes, so entities the user just talked about — and their facts — reach the
context whether or not the current question names them, not only from the
query's entities and the triples nearest to it. This module returns the
entities of the identity's newest memories (the session's, when the request
carries one), bounded, for the expansion to walk from as well.

One indexed read of `loom.memory FINAL`; pure SQL builder for the tests.
"""

from __future__ import annotations

from typing import Any

_SQL = """
SELECT arrayJoin(entities) AS entity, max(created_at) AS last_seen
FROM (
    SELECT entities, created_at
    FROM loom.memory FINAL
    WHERE org = {org:String} AND namespace = {ns:String}
      AND status = 'active' AND tombstoned_at IS NULL
      -- a row labelled high after the extractor ran keeps entities mined from its value
      AND sensitivity != 'high'
      AND notEmpty(entities)__SCOPE__
    ORDER BY created_at DESC
    LIMIT {n:UInt32}
)
GROUP BY entity
ORDER BY last_seen DESC, entity
LIMIT {lim:UInt32}
"""


def build_query(
    *,
    org: str,
    namespace: str,
    agent: str | None,
    user_id: str | None,
    session_id: str | None,
    memories: int,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    """The statement and parameters. `agent='*'`/None and `user_id`/`session_id`
    '-' or None widen the scope the way memory.search does."""
    scope = ""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "n": max(1, int(memories)),
        "lim": max(1, int(limit)),
    }
    if agent and agent not in ("*", "-"):
        scope += " AND agent = {agent:String}"
        params["agent"] = agent
    if user_id and user_id != "-":
        scope += " AND user_id = {uid:String}"
        params["uid"] = user_id
    if session_id and session_id != "-":
        scope += " AND session_id = {sid:String}"
        params["sid"] = session_id
    return _SQL.replace("__SCOPE__", scope), params


async def recent_seed_entities(
    *,
    org: str,
    namespace: str,
    agent: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    memories: int = 5,
    limit: int = 8,
    ch: Any | None = None,
) -> list[str]:
    """Entities of the newest `memories` rows in scope, most recent first,
    at most `limit`; [] on any failure — a seed is additive, never blocking."""
    from server.core.ch import get_client

    try:
        ch = ch or get_client()
        sql, params = build_query(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id,
            session_id=session_id,
            memories=memories,
            limit=limit,
        )
        rows = await ch.query_all(sql, params)
        out: list[str] = []
        for r in rows:
            e = str(r.get("entity") or "").strip().lower()
            if e and e not in out:
                out.append(e)
        return out
    except Exception:  # noqa: BLE001 — a recency seed never fails a search
        return []


__all__ = ["build_query", "recent_seed_entities"]
