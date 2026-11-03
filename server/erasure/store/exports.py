"""The reads behind `erasure.export`: a subject's rows per section."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.rows import (
    AgentEventExportRow,
    AuditExportRow,
    ChunkExportRow,
    ConversationExportRow,
    ConversationThreadExportRow,
    MemoryExportRow,
)

# Export-side reads (data subject access request).


@dataclass(frozen=True)
class Shown:
    """The namespaces an export may show its caller, applied in each read before
    its LIMIT so hidden rows never take a visible row's place: none of
    `hidden`, and of the collections only `collections` (None: every one)."""

    hidden: tuple[str, ...] = ()
    collections: tuple[str, ...] | None = None
    # `server.auth.sharing.COLLECTION_PREFIX`, which the handler passes (a store
    # imports no auth module).
    collection_prefix: str = "col-"

    def clause(self) -> tuple[str, dict[str, Any]]:
        sql = " AND namespace NOT IN {shown_hidden:Array(String)}"
        params: dict[str, Any] = {"shown_hidden": list(self.hidden)}
        if self.collections is not None:
            sql += (
                " AND (NOT startsWith(namespace, {shown_prefix:String})"
                " OR namespace IN {shown_collections:Array(String)})"
            )
            params["shown_prefix"] = self.collection_prefix
            params["shown_collections"] = list(self.collections)
        return sql, params


_EVERY = Shown()


async def export_memory(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    include_tombstoned: bool,
    limit: int,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> list[MemoryExportRow]:
    """Read loom.memory rows for one (org, user_id|namespace) scope.
    Returns +1 row beyond the requested limit so the caller can detect
    truncation."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    scope_clause = (
        "user_id = {scope:String}" if scope_type == "user_id" else "namespace = {scope:String}"
    )
    tomb_clause = "" if include_tombstoned else " AND tombstoned_at IS NULL"
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_id,
               memory_key, memory_type,
               value, confidence, source_trace, version, tombstoned_at, created_at,
               expires_at, sensitivity
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND {scope_clause}{tomb_clause}{shown_sql}
        ORDER BY namespace, agent, memory_key
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "scope": scope_value, "lim": int(limit), **shown_params},
    )
    # memory_id is a UUID column; the export carries it as a string.
    return cast("list[MemoryExportRow]", [{**r, "memory_id": str(r["memory_id"])} for r in rows])


async def export_knowledge(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    limit: int,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> list[ChunkExportRow]:
    """Read loom.chunks rows in one namespace, or the ones one user wrote."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    column = "user_id" if scope_type == "user_id" else "namespace"
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, source_uri, chunk_idx, content, context, tags, doc_metadata,
               chunk_metadata, ingested_at, doc_superseded_at, embedding_model,
               embedding_dim
        FROM loom.chunks
        WHERE org = {{org:String}} AND {column} = {{scope:String}}{shown_sql}
        ORDER BY namespace, source_uri, chunk_idx
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "scope": scope_value, "lim": int(limit), **shown_params},
    )
    return cast("list[ChunkExportRow]", rows)


# The text a conversation row carries, counted against the section's byte budget.
_CONVERSATION_BYTES = "length(content) + length(reasoning) + length(metadata)"
_CONVERSATION_ORDER = "namespace, thread_id, ts, msg_id"
_CONVERSATION_COLUMNS = (
    "org, namespace, agent, user_id, thread_id, msg_id, role, content, reasoning,"
    " metadata, tokens, run_id, ts, is_saved, saved_at, tombstoned_at"
)


async def export_conversations(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    include_tombstoned: bool,
    limit: int,
    max_bytes: int,
    as_of: datetime,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> tuple[list[ConversationExportRow], bool]:
    """Read loom.conversations rows (transcript turns and saved messages) in one
    namespace, or the ones one user owns, oldest first per thread: at most `limit`
    of them, and whether rows past `limit` or past `max_bytes` of text were left
    out. Only turns stamped no later than `as_of` (the export's start) are read.

    A turn has no size cap, so the rows stop once the text before a row reaches
    `max_bytes`. The first statement finds where they stop from the text lengths
    alone, and also returns the first row past the budget, which says more rows
    exist. The second reads the text of the rows up to that point. Neither reads a
    turn appended after `as_of`; one written meanwhile with an earlier stamp of its
    own can still be read, and is dropped, as the first statement never measured it.
    A measured turn missing from the read (pushed out by such a write, or deleted
    meanwhile) marks the section truncated, so the page can then even be empty."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    column = "user_id" if scope_type == "user_id" else "namespace"
    tomb_clause = "" if include_tombstoned else " AND tombstoned_at IS NULL"
    where = (
        f"org = {{org:String}} AND {column} = {{scope:String}}"
        f" AND ts <= {{as_of:DateTime64(3)}}{tomb_clause}{shown_sql}"
    )
    params: dict[str, Any] = {
        "org": org,
        "scope": scope_value,
        "as_of": as_of,
        **shown_params,
    }
    # A row's sums depend only on the rows before it, so the windows need only the
    # rows the page can hold, plus one.
    sizes = await ch.query_all(
        f"""
        SELECT namespace, thread_id, ts, msg_id, row_bytes, bytes_before
        FROM (
            SELECT namespace, thread_id, ts, msg_id, row_bytes,
                   sum(row_bytes) OVER (ORDER BY {_CONVERSATION_ORDER}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS bytes_before,
                   sum(row_bytes) OVER (ORDER BY {_CONVERSATION_ORDER}
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 2 PRECEDING) AS bytes_before_previous
            FROM (
                SELECT namespace, thread_id, ts, msg_id, {_CONVERSATION_BYTES} AS row_bytes
                FROM loom.conversations
                WHERE {where}
                ORDER BY {_CONVERSATION_ORDER}
                LIMIT {{lim:UInt64}}
            )
        )
        WHERE bytes_before_previous < {{max_bytes:UInt64}}
        ORDER BY {_CONVERSATION_ORDER}
        """,
        # One row past `limit` says more exist; its text is never read.
        params={**params, "lim": int(limit) + 1, "max_bytes": int(max_bytes)},
    )
    kept = [s for s in sizes if int(s["bytes_before"]) < max_bytes][:limit]
    if not kept:
        return [], False
    last = kept[-1]
    rows = await ch.query_all(
        f"""
        SELECT {_CONVERSATION_COLUMNS}, {_CONVERSATION_BYTES} AS row_bytes
        FROM loom.conversations
        WHERE {where}
          AND ({_CONVERSATION_ORDER}) <= ({{last_ns:String}}, {{last_tid:String}},
                                          {{last_ts:DateTime64(3)}}, {{last_mid:String}})
        ORDER BY {_CONVERSATION_ORDER}
        LIMIT {{kept:UInt64}}
        """,
        params={
            **params,
            "last_ns": last["namespace"],
            "last_tid": last["thread_id"],
            "last_ts": last["ts"],
            "last_mid": last["msg_id"],
            "kept": len(kept),
        },
    )
    # Keyed by size too: two copies of one turn tie on the sort key, and the read may
    # return a copy the size pass did not count.
    measured = Counter(_turn_key(s) for s in kept)
    served: list[dict[str, Any]] = []
    for r in rows:
        key = _turn_key(r)
        del r["row_bytes"]
        if measured[key] > 0:
            measured[key] -= 1
            served.append(r)
    left_out = len(kept) < len(sizes) or measured.total() > 0
    return cast("list[ConversationExportRow]", served), left_out


def _turn_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row["namespace"], row["thread_id"], row["ts"], row["msg_id"], int(row["row_bytes"]))


async def export_conversation_threads(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    include_tombstoned: bool,
    limit: int,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> list[ConversationThreadExportRow]:
    """Read loom.conversation_threads rows (titles and metadata) in one
    namespace, or the ones one user owns."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    column = "user_id" if scope_type == "user_id" else "namespace"
    tomb_clause = "" if include_tombstoned else " AND tombstoned_at IS NULL"
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, thread_id, title, metadata, run_id,
               created_at, updated_at, tombstoned_at
        FROM loom.conversation_threads FINAL
        WHERE org = {{org:String}} AND {column} = {{scope:String}}{tomb_clause}{shown_sql}
        ORDER BY namespace, thread_id
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "scope": scope_value, "lim": int(limit), **shown_params},
    )
    return cast("list[ConversationThreadExportRow]", rows)


async def export_agent_events(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    limit: int,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> list[AgentEventExportRow]:
    """Read loom.agent_events rows in one scope, newest-first."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    scope_clause = (
        "user_id = {scope:String}" if scope_type == "user_id" else "namespace = {scope:String}"
    )
    rows = await ch.query_all(
        f"""
        SELECT ts, org, namespace, agent, user_id, session_id, operation, request_id,
               trace_id, latency_ms, input_tokens,
               output_tokens, embedding_tokens, upstream_cost_usd, bytes_written,
               success, error_code, kind, args
        FROM loom.agent_events
        WHERE org = {{org:String}} AND {scope_clause}{shown_sql}
        ORDER BY ts DESC
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "scope": scope_value, "lim": int(limit), **shown_params},
    )
    return cast("list[AgentEventExportRow]", rows)


async def export_audit_namespace(
    org: str,
    namespace: str,
    *,
    limit: int,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> list[AuditExportRow]:
    """Audit rows tagged with this namespace."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    rows = await ch.query_all(
        f"""
        SELECT ts, event_type, org, namespace, actor, subject, request_id
        FROM loom.audit_log
        WHERE org = {{org:String}} AND namespace = {{ns:String}}{shown_sql}
        ORDER BY ts DESC
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit), **shown_params},
    )
    return cast("list[AuditExportRow]", rows)


async def export_audit_by_subject_substring(
    org: str,
    needle: str,
    *,
    limit: int,
    shown: Shown = _EVERY,
    ch: _CHClient | None = None,
) -> list[AuditExportRow]:
    """audit_log has no native user_id column; subject is a JSON blob.
    Substring-scan for user_id scope — coarse but adequate against the
    audit-log shape (the row is rewritten + re-hashed during erasure
    anyway, so a slightly-imprecise scan is fine)."""
    ch = ch or _default_client()
    shown_sql, shown_params = shown.clause()
    rows = await ch.query_all(
        f"""
        SELECT ts, event_type, org, namespace, actor, subject, request_id
        FROM loom.audit_log
        WHERE org = {{org:String}}
          AND position(subject, {{needle:String}}) > 0{shown_sql}
        ORDER BY ts DESC
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "needle": needle, "lim": int(limit), **shown_params},
    )
    return cast("list[AuditExportRow]", rows)
