"""Repository for loom.learnings_queue.

The human-review queue for verified-auto-promote candidates. RMT
collapses on (org, namespace, candidate_id); status transitions are
new INSERTs with bumped versions.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client


async def list_by_status(
    org: str,
    namespace: str,
    status: str,
    *,
    limit: int = 100,
    source_kind: str | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Page through the queue, newest first, filtered to one status.

    `source_kind`, when set, restricts to candidates from one seeder.
    """
    ch = ch or _default_client()
    extra = ""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "status": status,
        "lim": int(limit),
    }
    if source_kind is not None:
        extra = " AND source_kind = {sk:String}"
        params["sk"] = source_kind
    return await ch.query_all(
        f"""
        SELECT candidate_id, memory_key, memory_type, proposed_value,
               supporter_count, confidence_avg, supporters, rationale,
               source_kind, agent,
               status, proposed_at, decided_at, decided_by, decision_reason
        FROM loom.learnings_queue FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND status = {{status:String}}
          {extra}
        ORDER BY proposed_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


async def find_candidate(
    org: str,
    namespace: str,
    candidate_id: str,
    *,
    ch: _CHClient | None = None,
) -> dict[str, Any] | None:
    """Resolve one candidate row. learnings.review precheck (None → 404)
    and the candidate side of `memory.lineage`'s provenance chain.

    `seeded_from_*` is surfaced so lineage can point at the originating
    conversation turn; `supporter_count`, `confidence_avg`, `proposed_at`, `agent` and
    `filed_by_user` so a review's new version carries them forward.
    """
    ch = ch or _default_client()
    return await ch.query_one(
        """
        SELECT candidate_id, memory_key, memory_type, proposed_value,
               status, supporters, rationale, source_kind,
               seeded_from_thread_id, seeded_from_msg_id,
               supporter_count, confidence_avg, proposed_at,
               agent, filed_by_user
        FROM loom.learnings_queue FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND candidate_id = {cid:UUID}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "cid": candidate_id},
    )


async def insert_row(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """Insert a queue row; review writes a new row with the decided status."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.learnings_queue",
        [row],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


# loom.learnings_supporters' columns, in table order (tests/unit/test_assistant_learnings.py).
SUPPORTER_COLUMNS = (
    "org",
    "namespace",
    "candidate_id",
    "user_id",
    "session_id",
    "confidence",
    "created_at",
)


async def insert_supporter(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """One session's support for an `assistant_turn` candidate; a repeat of the same
    (candidate, user, session) collapses into it. Every column is written."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.learnings_supporters",
        [{c: row[c] for c in SUPPORTER_COLUMNS}],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def supporter_stats(
    org: str,
    namespace: str,
    candidate_ids: list[str],
    *,
    limit: int = 50,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per candidate: distinct users, distinct sessions, the mean confidence and the
    first `limit` (user_id, session_id) pairs by time."""
    ch = ch or _default_client()
    return await ch.query_all(
        """
        SELECT candidate_id,
               uniqExact(user_id) AS users,
               uniqExact(user_id, session_id) AS sessions,
               avg(confidence) AS confidence,
               arraySlice(arraySort(groupArray((created_at, user_id, session_id))),
                          1, {lim:UInt32}) AS pairs
        FROM loom.learnings_supporters FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND candidate_id IN {ids:Array(UUID)}
        GROUP BY candidate_id
        """,
        params={"org": org, "ns": namespace, "ids": candidate_ids, "lim": int(limit)},
    )
