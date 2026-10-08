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
               source_kind,
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
    conversation turn; `supporter_count`, `confidence_avg` and `proposed_at` so a review's
    new version carries them forward.
    """
    ch = ch or _default_client()
    return await ch.query_one(
        """
        SELECT candidate_id, memory_key, memory_type, proposed_value,
               status, supporters, rationale, source_kind,
               seeded_from_thread_id, seeded_from_msg_id,
               supporter_count, confidence_avg, proposed_at
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
