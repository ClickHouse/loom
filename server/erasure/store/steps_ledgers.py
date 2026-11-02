"""Erasure steps for the ledger, trace and queue tables: conversations, parked
context, session briefs, the idempotency cache, traces, events, tasks, agent
runs, the curation, consolidation and projection-failure ledgers, and the
learnings queue and its supporters."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from server.core.ch_params import chunks
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store.mutations import delete_scoped, namespaces_of, tombstone_scoped
from server.erasure.store.predicates import (
    NS_EQ,
    NS_IN,
    ORPHAN_LEARNINGS,
    PAYLOAD_UID,
    SUPPORTED_LEARNINGS,
    UID,
)
from server.observability import events_stream
from server.settings import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Steps for the ledger, trace and queue tables. Every `loom.*` table is a census row,
# a documented step-only table or a NO_MEMORY_CONTENT entry (the registry
# test holds schema.sql to the three lists). These steps reach the readable
# tables that fall outside both lists: raw conversation
# turns, parked context, search/exposure/code traces and the events stream
# they come from, the ingest drop ledger, code anchors, chunk-derived entity
# rows, agent runs, curation tables, the consolidation and
# projection-failure ledgers. The learnings queue and its supporters are
# census rows whose steps live here too. All are keyed on
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
    column, see its census row), and its cached query vectors (`_drop_vectors`).
    Returns the replay bodies deleted; the vectors' count is logged. A row with
    org '' leaves by the table's TTL."""
    del namespace  # the rows carry no namespace; the scope is the org
    n = await delete_scoped("loom.idempotency", org=org, scope_where="1", params={}, ch=ch)
    await _drop_vectors(org, ch=ch)
    return n


async def delete_idempotency_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    """Every cached replay body in the user's org, not only the rows whose
    stamped subject is `user_id`: a replay a service account stored while
    acting for the user quotes the user's text under the account's subject,
    and nothing on the row names the user; and the org's cached query vectors,
    which name no user either (`_drop_vectors`). Returns the replay bodies
    deleted; the vectors' count is logged. Fail closed; the cost is cache misses."""
    del user_id  # no row column names the end user; the scope is the org
    n = await delete_scoped("loom.idempotency", org=org, scope_where="1", params={}, ch=ch)
    await _drop_vectors(org, ch=ch)
    return n


async def _drop_vectors(org: str, *, ch: _CHClient | None) -> None:
    """The org's cached query vectors, beside its replay bodies: an uncounted
    table, so its count goes to the log rather than the step's."""
    n = await delete_embedding_cache_for_org(org, ch=ch)
    log.info("erasure: %d cached query vectors of org %s deleted", n, org)


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


async def _delete_events(
    org: str, scope_where: str, params: dict[str, Any], ch: _CHClient | None
) -> int:
    # A timer may have popped a batch already: wait for its insert before counting.
    await asyncio.wait_for(
        events_stream.flush_now(), timeout=settings.erasure_event_drain_timeout_s
    )
    return await delete_scoped(
        "loom.events", org=org, scope_where=scope_where, params=params, ch=ch
    )


async def delete_events_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    return await _delete_events(org, NS_EQ, {"ns": namespace}, ch)


async def delete_events_by_user(org: str, user_id: str, *, ch: _CHClient | None = None) -> int:
    return await _delete_events(org, UID, {"uid": user_id}, ch)


async def delete_memory_drops_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """The ingest drop ledger names the dropped item (a key or a field);
    30-day TTL, no soft-delete."""
    return await delete_scoped(
        "loom.memory_drops", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_namespace_guides_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Every revision of the namespace's guide: text its writers wrote about it."""
    return await delete_scoped(
        "loom.namespace_guides", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_delete_version_checks_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """memory.delete_version's owed checks name a row's identity and key; 30-day TTL."""
    return await delete_scoped(
        "loom.delete_version_checks",
        org=org,
        scope_where=NS_EQ,
        params={"ns": namespace},
        ch=ch,
    )


async def delete_delete_version_checks_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.delete_version_checks", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


async def delete_memory_webhook_outbox_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Memory webhook events name a row's identity, key and id; 3-day TTL. Their
    attempt rows hold only event ids and counters and are left to expire."""
    return await delete_scoped(
        "loom.memory_webhook_outbox", org=org, scope_where=NS_EQ, params={"ns": namespace}, ch=ch
    )


async def delete_memory_webhook_outbox_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    return await delete_scoped(
        "loom.memory_webhook_outbox", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )


_TASK_TABLES = ("loom.tasks", "loom.task_events")


async def delete_tasks_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Task briefs, notes and outcomes quote code and issue text.
    Both tables key on the inbox's owner namespace."""
    return await _delete_many(_TASK_TABLES, org, NS_EQ, {"ns": namespace}, ch)


_LEARNINGS_TABLES = ("loom.learnings_supporters", "loom.learnings_queue")


async def delete_learnings_queue_by_namespace(
    org: str, namespace: str | Sequence[str], *, ch: _CHClient | None = None
) -> int:
    """Promotion candidates quote the proposed value; their supporters name users."""
    return await _delete_many(_LEARNINGS_TABLES, org, NS_EQ, {"ns": namespace}, ch)


async def delete_learnings_queue_by_user(
    org: str, user_id: str, *, ch: _CHClient | None = None
) -> int:
    """The user's supporter rows, which hold every word of theirs the learnings queue
    keeps; then the bot-rule candidates left with no supporter
    (`predicates.ORPHAN_LEARNINGS`). A pending one is deleted. A decided one keeps
    its decision on a status row with the rule blanked, so a rejected rule never
    comes back as pending; its rows that still hold the rule are deleted after the
    blank row is written, so a retry finds whatever a failed step left.

    Each delete is bounded to what was read, so a review landing after the read
    survives (holding the rule) and the census's residue reopens this step to blank
    it: a pending candidate loses only its `pending` rows, a decided one only its
    rows that hold the rule at a version no newer than the one it read for that
    candidate. A pending candidate a supporter backed after the read is kept: each
    slice re-reads the supporter rows right before its delete, and a filing writes its
    supporter row before its candidate row. Only a supporter landing between that read
    and its delete can still lose its candidate."""
    ch = ch or _default_client()
    n = await delete_scoped(
        "loom.learnings_supporters", org=org, scope_where=UID, params={"uid": user_id}, ch=ch
    )
    rows = await ch.query_all(
        "SELECT DISTINCT candidate_id FROM loom.learnings_queue "
        f"WHERE org = {{org:String}} AND {ORPHAN_LEARNINGS}",
        params={"org": org},
    )
    # sorted, so a re-run cuts the same slices and finds its own pending mutations
    cids = sorted(str(r["candidate_id"]) for r in rows)
    if not cids:
        return n
    latest: list[dict[str, Any]] = []
    for part in chunks(cids):
        latest += await ch.query_all(
            "SELECT * FROM loom.learnings_queue FINAL "
            "WHERE org = {org:String} AND candidate_id IN {cids:Array(UUID)}",
            params={"org": org, "cids": part},
        )
    pending = sorted(str(r["candidate_id"]) for r in latest if r["status"] == "pending")
    decided = [r for r in latest if r["status"] != "pending"]
    blank = [
        {**r, "proposed_value": "", "version": int(r["version"]) + 1}
        for r in decided
        if r["proposed_value"]
    ]
    if blank:
        await ch.insert(
            "loom.learnings_queue", blank, settings={"async_insert": 0, "wait_for_async_insert": 1}
        )
    for part in chunks(pending):
        # read right before the delete, which names its ids: a mutation takes no subquery
        backed = await ch.query_all(
            f"SELECT DISTINCT toString(candidate_id) AS id FROM {SUPPORTED_LEARNINGS} "
            "WHERE candidate_id IN {cids:Array(UUID)}",
            params={"org": org, "cids": part},
        )
        unbacked = sorted(set(part) - {str(r["id"]) for r in backed})
        if unbacked:
            n += await delete_scoped(
                "loom.learnings_queue",
                org=org,
                scope_where="candidate_id IN {cids:Array(UUID)} AND status = 'pending'",
                params={"cids": unbacked},
                ch=ch,
            )
    decided_versions = sorted((str(r["candidate_id"]), int(r["version"])) for r in decided)
    for versions in chunks(decided_versions):
        n += await delete_scoped(
            "loom.learnings_queue",
            org=org,
            # Each candidate is bounded by its own version read, never another's.
            scope_where=(
                "proposed_value != '' AND arrayExists(r -> r.1 = candidate_id "
                "AND version <= r.2, {decided:Array(Tuple(UUID, UInt64))})"
            ),
            params={"decided": versions},
            ch=ch,
        )
    return n


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


async def delete_embedding_cache_for_org(org: str, *, ch: _CHClient | None = None) -> int:
    """Every cached query vector the org's requests left. The rows hold
    vectors of query text, never the text, and have no namespace or user axis,
    so any erasure in the org clears them all, as it clears the search trace's
    query text (a cache: the cost is misses). Rows a build before `org` wrote
    carry '', which this build's reads never find; the 30-day TTL takes them."""
    return await delete_scoped(
        "loom.embedding_cache", org=org, scope_where="1", params={}, ch=ch, lightweight=True
    )
