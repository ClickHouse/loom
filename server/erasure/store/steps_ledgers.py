"""Erasure steps for the tables outside the census: conversations, parked
context, session briefs, the idempotency cache, traces, events, tasks, agent
runs and the curation, consolidation and projection-failure ledgers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store.mutations import delete_scoped, namespaces_of, tombstone_scoped
from server.erasure.store.predicates import NS_EQ, NS_IN, PAYLOAD_UID, UID

# ---------------------------------------------------------------------------
# Steps for tables outside the census. Every `loom.*` table is a census row,
# a documented step-only table or a NO_MEMORY_CONTENT entry (the registry
# test holds schema.sql to the three lists). These steps reach the readable
# tables that fall outside both lists: raw conversation
# turns, parked context, search/exposure/code traces and the events stream
# they come from, the ingest drop ledger, code anchors, chunk-derived entity
# rows, the learnings queue, agent runs, curation tables, the
# consolidation and projection-failure ledgers. All are keyed on
# (org, namespace) and reach the user axis where the table carries one.
# ---------------------------------------------------------------------------

# Raw turns: a plain MergeTree with `tombstoned_at` (no FINAL); the thread and
# embedding tables are RMTs.
_CONVERSATION_TOMBSTONE = (
    ("loom.conversations", False),
    ("loom.conversation_threads", True),
    ("loom.conversation_embeddings", True),
)


async def _conversations_scoped(
    org: str, scope_where: str, params: dict[str, Any], ch: _CHClient | None
) -> int:
    total = 0
    for table, final in _CONVERSATION_TOMBSTONE:
        total += await tombstone_scoped(
            table, org=org, scope_where=scope_where, params=params, ch=ch, final=final
        )
    return total


async def tombstone_conversations_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await _conversations_scoped(org, NS_EQ, {"ns": namespace}, ch)


async def tombstone_conversations_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await _conversations_scoped(org, UID, {"uid": user_id}, ch)


async def tombstone_context_blobs_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Parked context (context.offload): the caller's own text under its
    identity, RMT with `tombstoned_at`; expires on its own TTL otherwise."""
    return await tombstone_scoped(
        "loom.context_blobs", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def tombstone_context_blobs_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await tombstone_scoped(
        "loom.context_blobs", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


# A session brief condenses a session's work — its goal, decisions, errors
# and file paths — so it is memory content. The current
# revision (RMT) and the append-only history (MergeTree, filled by a view)
# both carry the user axis; erasure deletes both outright rather than
# tombstoning, because the history table has no tombstone to hide behind.
_SESSION_BRIEF_TABLES = ("loom.session_briefs", "loom.session_brief_revisions")


async def _session_briefs_scoped(
    org: str, scope_where: str, params: dict[str, Any], ch: _CHClient | None
) -> int:
    total = 0
    for table in _SESSION_BRIEF_TABLES:
        total += await delete_scoped(table, org=org, scope_where=scope_where, params=params, ch=ch)
    return total


async def delete_session_briefs_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await _session_briefs_scoped(org, NS_EQ, {"ns": namespace}, ch)


async def delete_session_briefs_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await _session_briefs_scoped(org, UID, {"uid": user_id}, ch)


async def _delete_many(
    tables: Sequence[str], org: str, scope_where: str, params: dict[str, Any], ch: _CHClient | None
) -> int:
    total = 0
    for table in tables:
        total += await delete_scoped(table, org=org, scope_where=scope_where, params=params, ch=ch)
    return total


async def delete_idempotency_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The org's cached replay bodies (loom.idempotency has no namespace
    column, see its census row). A row with org '' leaves by the table's
    24-hour TTL."""
    del namespace  # the rows carry no namespace; the scope is the org
    return await delete_scoped("loom.idempotency", org=org, scope_where="1", params={}, ch=ch)


async def delete_idempotency_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Every cached replay body in the user's org, not only the rows whose
    stamped subject is `user_id`: a replay a service account stored while
    acting for the user quotes the user's text under the account's subject,
    and nothing on the row names the user. Fail closed; the cost is cache
    misses."""
    del user_id  # no row column names the end user; the scope is the org
    return await delete_scoped("loom.idempotency", org=org, scope_where="1", params={}, ch=ch)


# Telemetry that quotes the subject: the search trace keeps the query text and
# the served keys, the exposure ledger the served keys per actor, the code
# trace the query and the served paths, and `loom.events` is the stream every
# one of them is projected from — its `payload` carries the same text for its
# 30-day TTL. MergeTree, no soft-delete → ALTER DELETE.
async def delete_search_trace_log_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.search_trace_log", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_memory_exposures_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.memory_exposures", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_memory_exposures_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.memory_exposures", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


_CODE_TRACE_TABLES_NS = ("loom.code_search_trace", "loom.code_search_outcome")


async def delete_code_search_trace_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await _delete_many(_CODE_TRACE_TABLES_NS, org, NS_EQ, {"ns": namespace}, ch)


async def delete_code_search_trace_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    # code_search_outcome has no user axis; the trace row does.
    return await delete_scoped(
        "loom.code_search_trace", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


async def delete_events_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.events", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_events_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    return await delete_scoped(
        "loom.events", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


async def delete_memory_drops_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The ingest drop ledger names the dropped item (a key or a field);
    30-day TTL, no soft-delete."""
    return await delete_scoped(
        "loom.memory_drops", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


_TASK_TABLES = ("loom.tasks", "loom.task_events")


async def delete_tasks_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Task briefs, notes and outcomes quote code and issue text.
    Both tables key on the inbox's owner namespace."""
    return await _delete_many(_TASK_TABLES, org, NS_EQ, {"ns": namespace}, ch)


async def delete_learnings_queue_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Promotion candidates quote the proposed value and its supporters."""
    return await delete_scoped(
        "loom.learnings_queue", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def _delete_run_claims(
    org: str, scope_where: str, params: dict[str, Any], ch: _CHClient | None
) -> None:
    """agent_run_claims: the run ids and user ids of runs not yet closed. KeeperMap,
    so a key drop rather than a part mutation (never in system.mutations)."""
    ch = ch or _default_client()
    await erasure_mutations.run_keeper_delete(
        "loom.agent_run_claims",
        where=f"org = {{org:String}} AND {scope_where}",
        params={"org": org, **params},
        ch=ch,
    )


async def delete_agent_runs_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """agent_runs: goal, user_request and final_response per run; RMT
    without a soft-delete column. The runs' claims go with them."""
    await _delete_run_claims(org, NS_IN, {"ns": namespaces_of(namespace)}, ch)
    return await delete_scoped(
        "loom.agent_runs", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_agent_runs_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    await _delete_run_claims(org, UID, {"uid": user_id}, ch)
    return await delete_scoped(
        "loom.agent_runs", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


# Curation tables name the namespace's memory keys (and, for operator edges,
# an explanation and evidence text); admin-authored, no user axis; all three
# are tombstoned through `tombstoned_at`.
_CURATION_TOMBSTONE = (
    "loom.operator_edges",
    "loom.conflict_allowlist",
    "loom.memory_categories",
)


async def tombstone_curation_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    total = 0
    for table in _CURATION_TOMBSTONE:
        total += await tombstone_scoped(
            table, org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
        )
    return total


async def delete_consolidation_runs_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The merge ledger: source and output keys per run, 180-day TTL."""
    return await delete_scoped(
        "loom.consolidation_runs", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_consolidation_runs_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.consolidation_runs", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


async def delete_projection_failures_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """A failed projection keeps its payload (the row it could not write) and the keys.
    Resolved rows leave by the table's TTL; active and abandoned ones stay until erased."""
    return await delete_scoped(
        "loom.memory_projection_failures",
        org=org,
        scope_where=NS_EQ,
        params={"ns": namespace},
        ch=ch,
    )


async def delete_projection_failures_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """By the identity its payload names: the entries a bulk write could not project."""
    return await delete_scoped(
        "loom.memory_projection_failures",
        org=org,
        scope_where=PAYLOAD_UID,
        params={"uid": user_id},
        ch=ch,
    )
