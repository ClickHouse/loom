"""The reads behind `erasure.export`: a subject's rows per section."""

from __future__ import annotations

from typing import cast

from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.rows import (
    AgentEventExportRow,
    AuditExportRow,
    ChunkExportRow,
    MemoryExportRow,
)

# Export-side reads (data subject access request).


async def export_memory(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    include_tombstoned: bool,
    limit: int,
    ch: _CHClient | None = None,
) -> list[MemoryExportRow]:
    """Read loom.memory rows for one (org, user_id|namespace) scope.
    Returns +1 row beyond the requested limit so the caller can detect
    truncation."""
    ch = ch or _default_client()
    scope_clause = (
        "user_id = {scope:String}" if scope_type == "user_id" else "namespace = {scope:String}"
    )
    tomb_clause = "" if include_tombstoned else " AND tombstoned_at IS NULL"
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key, memory_type,
               value, confidence, source_trace, version, tombstoned_at, created_at,
               expires_at, sensitivity
        FROM loom.memory FINAL
        WHERE org = {{org:String}} AND {scope_clause}{tomb_clause}
        ORDER BY namespace, agent, memory_key
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "scope": scope_value, "lim": int(limit)},
    )
    return cast("list[MemoryExportRow]", rows)


async def export_knowledge(
    org: str,
    namespace: str,
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> list[ChunkExportRow]:
    """Read loom.chunks rows in one namespace. Knowledge is
    namespace-scoped; per-user export is empty (chunks don't track
    authorship)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT org, namespace, source_uri, chunk_idx, content, tags, doc_metadata,
               chunk_metadata, ingested_at, doc_superseded_at, embedding_model,
               embedding_dim
        FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
        ORDER BY source_uri, chunk_idx
        LIMIT {lim:UInt64}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )
    return cast("list[ChunkExportRow]", rows)


async def export_agent_events(
    org: str,
    scope_type: str,
    scope_value: str,
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> list[AgentEventExportRow]:
    """Read loom.agent_events rows in one scope, newest-first."""
    ch = ch or _default_client()
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
        WHERE org = {{org:String}} AND {scope_clause}
        ORDER BY ts DESC
        LIMIT {{lim:UInt64}}
        """,
        params={"org": org, "scope": scope_value, "lim": int(limit)},
    )
    return cast("list[AgentEventExportRow]", rows)


async def export_audit_namespace(
    org: str,
    namespace: str,
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> list[AuditExportRow]:
    """Audit rows tagged with this namespace."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT ts, event_type, org, namespace, actor, subject, request_id
        FROM loom.audit_log
        WHERE org = {org:String} AND namespace = {ns:String}
        ORDER BY ts DESC
        LIMIT {lim:UInt64}
        """,
        params={"org": org, "ns": namespace, "lim": int(limit)},
    )
    return cast("list[AuditExportRow]", rows)


async def export_audit_by_subject_substring(
    org: str,
    needle: str,
    *,
    limit: int,
    ch: _CHClient | None = None,
) -> list[AuditExportRow]:
    """audit_log has no native user_id column; subject is a JSON blob.
    Substring-scan for user_id scope — coarse but adequate against the
    audit-log shape (the row is rewritten + re-hashed during erasure
    anyway, so a slightly-imprecise scan is fine)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT ts, event_type, org, namespace, actor, subject, request_id
        FROM loom.audit_log
        WHERE org = {org:String}
          AND position(subject, {needle:String}) > 0
        ORDER BY ts DESC
        LIMIT {lim:UInt64}
        """,
        params={"org": org, "needle": needle, "lim": int(limit)},
    )
    return cast("list[AuditExportRow]", rows)
