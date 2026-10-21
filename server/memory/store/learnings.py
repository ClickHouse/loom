"""Repository for loom.learnings_queue and loom.learnings_supporters.

learnings_queue holds one ReplacingMergeTree row per candidate version,
collapsed on (org, namespace, candidate_id); a review writes a newer
version. learnings_supporters holds one append-only row per (candidate,
user, session) behind an `assistant_turn` rule, with that user's quotes.
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
    conversation turn; `supporter_count`, `confidence_avg`, `proposed_at` and `agent`
    so a review's new version carries them forward.
    """
    ch = ch or _default_client()
    return await ch.query_one(
        """
        SELECT candidate_id, memory_key, memory_type, proposed_value,
               status, supporters, rationale, source_kind,
               seeded_from_thread_id, seeded_from_msg_id,
               supporter_count, confidence_avg, proposed_at,
               agent
        FROM loom.learnings_queue FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND candidate_id = {cid:UUID}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "cid": candidate_id},
    )


async def rule_promoted(
    org: str,
    namespace: str,
    agent: str,
    memory_key: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether an `assistant_turn` rule of this bot under this key is served: its
    `learning://<candidate_id>` document is live. Only a knowledge promotion writes
    that document, and a later reject leaves it, so the status is not read. A rule
    whose document was deleted no longer counts, so a revised rule under its key is
    filed again."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT count() AS n FROM loom.documents FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri IN (
            SELECT concat('learning://', toString(candidate_id))
            FROM loom.learnings_queue FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND memory_key = {k:String}
              AND source_kind = 'assistant_turn'
          )
          AND superseded_at IS NULL
        """,
        params={"org": org, "ns": namespace, "agent": agent, "k": memory_key},
    )
    return bool(row and int(row["n"]))


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


# The supporter entries learnings.list returns per `assistant_turn` candidate.
_SUPPORTER_PAIRS = 50  # not a setting: a bound on one wire field's length

# loom.learnings_supporters' columns, in table order (tests/unit/test_assistant_learnings.py).
SUPPORTER_COLUMNS = (
    "org",
    "namespace",
    "agent",
    "candidate_id",
    "user_id",
    "session_id",
    "actor",
    "confidence",
    "asked",
    "said",
    "created_at",
)


async def insert_supporter(
    row: dict[str, Any],
    *,
    ch: _CHClient | None = None,
) -> None:
    """One session's support for an `assistant_turn` candidate, with that user's own
    quotes. Append-only; every column is written."""
    ch = ch or _default_client()
    await ch.insert(
        "loom.learnings_supporters",
        [{c: row[c] for c in SUPPORTER_COLUMNS}],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def supporter_exists(
    org: str,
    namespace: str,
    candidate_id: str,
    user_id: str,
    session_id: str,
    *,
    ch: _CHClient | None = None,
) -> bool:
    """Whether this (user, session) already backs the candidate."""
    ch = ch or _default_client()
    row = await ch.query_one(
        """
        SELECT count() AS n FROM loom.learnings_supporters
        WHERE org = {org:String} AND namespace = {ns:String}
          AND candidate_id = {cid:UUID}
          AND user_id = {uid:String} AND session_id = {sid:String}
        """,
        params={
            "org": org,
            "ns": namespace,
            "cid": candidate_id,
            "uid": user_id,
            "sid": session_id,
        },
    )
    return bool(row and int(row["n"]))


async def supporter_stats(
    org: str,
    namespace: str,
    candidate_ids: list[str],
    *,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Per candidate: distinct users, distinct sessions, distinct writers (the
    principals that wrote the rows: one token can send many user_ids), the mean
    confidence, the
    first `_SUPPORTER_PAIRS` (user_id, session_id) pairs by time and the earliest
    remaining supporter's quotes.

    A (user, session) counts once even when two racing filings both wrote its row:
    the inner query keeps its earliest row. The quotes are one row's pair, the
    earliest by (time, user_id, session_id), so a tie never mixes two users' words."""
    ch = ch or _default_client()
    return await ch.query_all(
        f"""
        SELECT candidate_id,
               uniqExact(user_id) AS users,
               count() AS sessions,
               uniqExact(a) AS writers,
               avg(c) AS confidence,
               groupArraySorted({_SUPPORTER_PAIRS})((t, user_id, session_id)) AS pairs,
               argMin(q, (t, user_id, session_id)) AS quotes,
               quotes.1 AS asked,
               quotes.2 AS said
        FROM (
            SELECT candidate_id, user_id, session_id,
                   min(created_at) AS t,
                   argMin((asked, said), created_at) AS q,
                   argMin(actor, created_at) AS a,
                   argMin(confidence, created_at) AS c
            FROM loom.learnings_supporters
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND candidate_id IN {{ids:Array(UUID)}}
            GROUP BY candidate_id, user_id, session_id
        )
        GROUP BY candidate_id
        """,
        params={"org": org, "ns": namespace, "ids": candidate_ids},
    )
