"""conversation.search vector-ranking service.

Vector search over loom.conversation_embeddings: embed the query, scan
filtered rows, rank by cosineDistance ascending (ties broken by recency).
The substring fallback lives on conversation_repo — a different query
shape (no vector), not a service concern.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from server.core.repo_base import _default_client
from server.core.vectors.search_settings import vector_search_settings
from server.knowledge.domain.conversations import ConversationRole

# Characters of a turn a hit shows (the embedding row keeps the whole embedded text).
HIT_EXCERPT_CHARS = 240


class _CHClient(Protocol):
    async def query_all(
        self,
        sql: str,
        params: dict[str, Any] | None = ...,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ConversationVectorQuery:
    org: str
    namespace: str
    agent: str
    embedding_model: str
    query_vec: list[float]
    top_k: int
    thread_id: str | None = None
    role: str | None = None
    run_id: str | None = None
    # Max cosine distance kept (1 - min_similarity). Default 2.0 = no gate
    # (full distance range), so callers that don't set it are unaffected.
    max_distance: float = 2.0


@dataclass(frozen=True)
class ConversationHit:
    thread_id: str
    msg_id: str
    role: ConversationRole
    content_excerpt: str
    ts: datetime
    distance: float


async def vector_search(
    q: ConversationVectorQuery,
    *,
    ch: _CHClient | None = None,
) -> list[ConversationHit]:
    """Vector search over loom.conversation_embeddings. Close-distance ties
    are ordered most-recent-first (ts DESC).
    """
    ch = ch or _default_client()
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "embedding_model = {model:String}",
        "embedding_dim > 0",
        "tombstoned_at IS NULL",
    ]
    params: dict[str, Any] = {
        "org": q.org,
        "ns": q.namespace,
        "model": q.embedding_model,
        "q": q.query_vec,
        "lim": int(q.top_k),
        "excerpt": HIT_EXCERPT_CHARS,
        "maxd": float(q.max_distance),
    }
    # Relevance gate: drop rows beyond max_distance so a short query doesn't
    # return every thread in the namespace ranked by distance.
    where.append("cosineDistance(embedding, {q:Array(Float32)}) <= {maxd:Float64}")
    # Explicit agent filters to that agent; "*"/"-" spans all agents — mirrors
    # memory.search so an operator browse sees every agent's threads.
    if q.agent and q.agent not in ("*", "-"):
        where.append("agent = {agent:String}")
        params["agent"] = q.agent
    if q.thread_id:
        where.append("thread_id = {thread_id:String}")
        params["thread_id"] = q.thread_id
    if q.role:
        where.append("role = {role:String}")
        params["role"] = q.role
    if q.run_id is not None:
        where.append("run_id = {run_id:String}")
        params["run_id"] = q.run_id

    # Single bare-distance sort key, ASC: a two-key sort (`ORDER BY distance
    # ASC, ts DESC`) disqualifies the query from CH's vector-search rewrite, so
    # the recency tie-break runs in Python below, where it costs nothing (top_k
    # rows, already distance-ordered by CH, and Python's sort is stable).
    rows = await ch.query_all(
        f"""
        SELECT thread_id, msg_id, role,
               substringUTF8(content_excerpt, 1, {{excerpt:UInt32}}) AS excerpt, ts,
               cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
        FROM loom.conversation_embeddings FINAL
        WHERE {" AND ".join(where)}
        ORDER BY distance ASC
        LIMIT {{lim:UInt32}}
        {vector_search_settings(table="loom.conversation_embeddings")}
        """,
        params=params,
    )
    hits = [
        ConversationHit(
            thread_id=r["thread_id"],
            msg_id=r["msg_id"],
            role=r["role"],
            content_excerpt=r["excerpt"],
            ts=r["ts"],
            distance=float(r["distance"]),
        )
        for r in rows
    ]
    # Recency breaks exact distance ties (newest first).
    hits.sort(key=lambda h: (h.distance, _ts_desc_key(h.ts)))
    return hits


def _ts_desc_key(ts: Any) -> float:
    """Negated epoch seconds so a plain ascending sort orders newest-first.

    `ts` arrives as a datetime from the CH driver, but tolerate a string or
    None rather than raising inside a sort comparator.
    """
    if isinstance(ts, datetime):
        return -ts.timestamp()
    try:
        return -datetime.fromisoformat(str(ts)).timestamp()
    except (TypeError, ValueError):
        return 0.0
