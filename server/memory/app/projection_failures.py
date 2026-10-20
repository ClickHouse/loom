"""Durable logging for best-effort memory projection failures."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.llm import customer
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult


@dataclass(frozen=True)
class ProjectionFailureRow:
    failure_id: str
    org: str
    namespace: str
    agent: str
    projection: str
    source_operation: str
    source_resource_id: str
    memory_keys: list[str]
    error_type: str
    error_message: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "active"
    created_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True)
class ProjectionFailureRetryResult:
    failure_id: str
    projection: str
    retried: bool
    resolved: bool
    reason: str = ""
    error: str = ""


async def record_projection_failure(
    *,
    org: str,
    namespace: str,
    agent: str = "",
    projection: str,
    source_operation: str = "",
    source_resource_id: str = "",
    memory_keys: list[str] | None = None,
    error: BaseException | str,
    payload: dict[str, Any] | None = None,
    ch: Any | None = None,
) -> None:
    """Append one projection failure row.

    Callers should wrap this helper in their own fail-open guard. This module
    intentionally does not import logging to avoid recursive logging loops in
    exception handlers.
    """

    ch = ch or get_client()
    now = naive_utc_now()
    error_type = type(error).__name__ if isinstance(error, BaseException) else "Error"
    error_message = str(error)
    await ch.insert(
        "loom.memory_projection_failures",
        [
            {
                "failure_id": str(uuid.uuid4()),
                "org": org,
                "namespace": namespace,
                "agent": agent,
                "projection": projection,
                "source_operation": source_operation,
                "source_resource_id": source_resource_id,
                "memory_keys": list(memory_keys or []),
                "error_type": error_type,
                "error_message": error_message[:4000],
                "payload": json.dumps(payload or {}, sort_keys=True, default=str),
                "status": "active",
                "created_at": now,
                "resolved_at": None,
            }
        ],
        # Committed: the row is the only record of the lost projection, and a
        # fire-and-forget buffer dies with the replica that holds it.
        settings={"async_insert": 1, "wait_for_async_insert": 1},
    )


def _decode_payload(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    try:
        obj = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _row_from_ch(row: dict[str, Any]) -> ProjectionFailureRow:
    return ProjectionFailureRow(
        failure_id=str(row.get("failure_id") or ""),
        org=str(row.get("org") or ""),
        namespace=str(row.get("namespace") or ""),
        agent=str(row.get("agent") or ""),
        projection=str(row.get("projection") or ""),
        source_operation=str(row.get("source_operation") or ""),
        source_resource_id=str(row.get("source_resource_id") or ""),
        memory_keys=[str(k) for k in list(row.get("memory_keys") or []) if str(k)],
        error_type=str(row.get("error_type") or ""),
        error_message=str(row.get("error_message") or ""),
        payload=_decode_payload(row.get("payload")),
        status=str(row.get("status") or "active"),
        created_at=(c if isinstance(c := row.get("created_at"), datetime) else None),
        resolved_at=(r if isinstance(r := row.get("resolved_at"), datetime) else None),
    )


async def list_projection_failures(
    *,
    org: str,
    namespace: str | None = None,
    projection: str = "",
    status: str = "active",
    limit: int = 100,
    ch: Any | None = None,
) -> list[ProjectionFailureRow]:
    """List durable projection failures newest-first."""

    ch = ch or get_client()
    where = ["org = {org:String}"]
    params: dict[str, Any] = {"org": org, "lim": int(limit)}
    if namespace:
        where.append("namespace = {ns:String}")
        params["ns"] = namespace
    if projection:
        where.append("projection = {projection:String}")
        params["projection"] = projection
    if status:
        where.append("status = {status:String}")
        params["status"] = status
    rows = await ch.query_all(
        f"""
        SELECT
            toString(failure_id) AS failure_id,
            org,
            namespace,
            agent,
            projection,
            source_operation,
            source_resource_id,
            memory_keys,
            error_type,
            error_message,
            payload,
            status,
            created_at,
            resolved_at
        FROM loom.memory_projection_failures
        WHERE {" AND ".join(where)}
        ORDER BY created_at DESC, failure_id DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [_row_from_ch(dict(row)) for row in rows]


async def mark_projection_failures_resolved(
    *,
    org: str,
    failure_ids: list[str],
    ch: Any | None = None,
) -> int:
    """Mark failures resolved with an ALTER UPDATE.

    ClickHouse updates are asynchronous on large tables, but this table is a
    small operator queue and the mutation is narrow by UUID.
    """

    ids = [fid for fid in failure_ids if fid]
    if not ids:
        return 0
    ch = ch or get_client()
    await ch.execute(
        """
        ALTER TABLE loom.memory_projection_failures
        UPDATE status = 'resolved', resolved_at = now()
        WHERE org = {org:String}
          AND toString(failure_id) IN {failure_ids:Array(String)}
          AND status = 'active'
        """,
        params={"org": org, "failure_ids": ids},
    )
    return len(ids)


async def retry_projection_failure(
    failure: ProjectionFailureRow,
    *,
    ch: Any | None = None,
) -> ProjectionFailureRetryResult:
    """Retry one supported projection failure and resolve it on success."""

    ch = ch or get_client()
    try:
        if failure.projection == "memory_belief_sources":
            await _retry_belief_sources(failure, ch=ch)
        elif failure.projection == "memory_chunks":
            await _retry_memory_chunks(failure, ch=ch)
        elif failure.projection == "memory_chunk_sources":
            await _retry_memory_chunk_sources(failure, ch=ch)
        elif failure.projection in {
            "entity_vectors",
            "entity_edges",
            "entity_triples",
        }:
            await _retry_graph_projection(failure, ch=ch)
        else:
            return ProjectionFailureRetryResult(
                failure_id=failure.failure_id,
                projection=failure.projection,
                retried=False,
                resolved=False,
                reason="unsupported_projection",
            )
        await mark_projection_failures_resolved(
            org=failure.org,
            failure_ids=[failure.failure_id],
            ch=ch,
        )
        return ProjectionFailureRetryResult(
            failure_id=failure.failure_id,
            projection=failure.projection,
            retried=True,
            resolved=True,
        )
    except customer.HOLDS:
        # Held by the model-call switch: no attempt, no error; the row stays open.
        return ProjectionFailureRetryResult(
            failure_id=failure.failure_id,
            projection=failure.projection,
            retried=False,
            resolved=False,
            reason="model_calls_held",
        )
    except Exception as e:  # noqa: BLE001 — reported on the result; the row stays open
        return ProjectionFailureRetryResult(
            failure_id=failure.failure_id,
            projection=failure.projection,
            retried=True,
            resolved=False,
            error=f"{type(e).__name__}: {e}",
        )


async def retry_projection_failures(
    *,
    org: str,
    namespace: str | None = None,
    projection: str = "",
    limit: int = 50,
    ch: Any | None = None,
) -> list[ProjectionFailureRetryResult]:
    """Retry active failures in newest-first order."""

    ch = ch or get_client()
    rows = await list_projection_failures(
        org=org,
        namespace=namespace,
        projection=projection,
        status="active",
        limit=limit,
        ch=ch,
    )
    out: list[ProjectionFailureRetryResult] = []
    for row in rows:
        out.append(await retry_projection_failure(row, ch=ch))
    return out


async def scan_active_projection_failures(
    *,
    limit: int = 50,
    exclude_orgs: frozenset[str] = frozenset(),
    ch: Any | None = None,
) -> list[ProjectionFailureRow]:
    """Return active failures across orgs for the repair worker; none of
    `exclude_orgs` (orgs whose model calls are switched off), which wait."""

    ch = ch or get_client()
    rows = await ch.query_all(
        """
        SELECT
            toString(failure_id) AS failure_id,
            org,
            namespace,
            agent,
            projection,
            source_operation,
            source_resource_id,
            memory_keys,
            error_type,
            error_message,
            payload,
            status,
            created_at,
            resolved_at
        FROM loom.memory_projection_failures
        WHERE status = 'active' AND NOT has({off:Array(String)}, org)
        ORDER BY created_at ASC, failure_id ASC
        LIMIT {lim:UInt32}
        """,
        params={"lim": int(limit), "off": sorted(exclude_orgs)},
    )
    return [_row_from_ch(dict(row)) for row in rows]


async def _retry_belief_sources(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    evidence_ids = [str(eid) for eid in list(failure.payload.get("evidence_ids") or []) if str(eid)]
    if not evidence_ids or not failure.memory_keys:
        raise ValueError("memory_belief_sources retry requires memory_keys and evidence_ids")
    rows = await ch.query_all(
        """
        SELECT memory_key, toString(memory_id) AS memory_id
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "keys": failure.memory_keys,
        },
    )
    links = [
        (str(row["memory_key"]), str(row["memory_id"])) for row in rows if row.get("memory_id")
    ]
    if not links:
        raise ValueError("no live memories found for belief-source retry")
    from server.memory.app.evidence import link_belief_sources

    await link_belief_sources(
        org=failure.org,
        namespace=failure.namespace,
        memory_links=links,
        evidence_ids=evidence_ids,
        support_type="extracted_from",
        ch=ch,
    )


async def _retry_memory_chunks(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    rows = await ch.query_all(
        """
        SELECT role, content, user_id
        FROM (
            SELECT role, content, user_id, observed_at, evidence_id, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND agent = {agent:String}
              AND source_resource_id = {source:String}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY observed_at ASC, evidence_id ASC
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "source": failure.source_resource_id,
        },
    )
    msgs = [
        {"role": str(row.get("role") or "user"), "content": str(row.get("content") or "")}
        for row in rows
        if str(row.get("content") or "").strip()
    ]
    if not msgs:
        raise ValueError("memory_chunks retry requires source evidence rows")
    # The chunks belong to the end user the source turns were written under;
    # turns of one source under several users leave the sentinel (a
    # user-bounded read then excludes the chunks rather than guess).
    users = {str(row.get("user_id") or "-") for row in rows}
    user_id = users.pop() if len(users) == 1 else "-"

    from server.memory.app.chunk_projection import write_memory_chunks

    retry_input = type(
        "_ProjectionRetryInput",
        (),
        {
            "org": failure.org,
            "namespace": failure.namespace,
            "agent": failure.agent,
            "user_id": user_id,
            "session_id": _session_from_source_resource_id(failure.source_resource_id),
        },
    )()
    await write_memory_chunks(
        retry_input,
        msgs,
        parent_memory_keys=failure.memory_keys,
    )


async def _retry_memory_chunk_sources(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    source = str(failure.payload.get("source_resource_id") or failure.source_resource_id)
    session_id = str(failure.payload.get("session_id") or _session_from_source_resource_id(source))
    chunk_rows = await ch.query_all(
        """
        SELECT
            chunk_index,
            content_sha256,
            line_start,
            line_end
        FROM loom.memory_chunks FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND agent = {agent:String}
          AND session_id = {sid:String}
          AND tombstoned_at IS NULL
        ORDER BY chunk_index ASC, content_sha256 ASC
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "sid": session_id,
        },
    )
    if not chunk_rows:
        raise ValueError("memory_chunk_sources retry requires chunk rows")
    evidence_rows = await ch.query_all(
        """
        SELECT toString(evidence_id) AS evidence_id, role, content
        FROM (
            SELECT evidence_id, role, content, observed_at, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND agent = {agent:String}
              AND source_resource_id = {source:String}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY observed_at ASC, evidence_id ASC
        """,
        params={
            "org": failure.org,
            "ns": failure.namespace,
            "agent": failure.agent,
            "source": source,
        },
    )
    msgs = [
        {"role": str(row.get("role") or "user"), "content": str(row.get("content") or "")}
        for row in evidence_rows
    ]
    evidence_ids = [str(row.get("evidence_id") or "") for row in evidence_rows]
    from server.memory.app.chunk_projection import message_line_evidence_refs

    refs = message_line_evidence_refs(msgs, evidence_ids)
    now = naive_utc_now()
    # The same builder as the write path.
    from server.memory.app.chunk_links import SOURCES_TABLE
    from server.memory.app.chunk_links import provenance_rows as _provenance_rows

    rows = _provenance_rows(
        org=failure.org,
        namespace=failure.namespace,
        agent=failure.agent,
        session_id=session_id,
        chunks=[
            (
                int(chunk.get("chunk_index") or 0),
                str(chunk.get("content_sha256") or ""),
                int(chunk.get("line_start") or 0),
                int(chunk.get("line_end") or 0),
            )
            for chunk in chunk_rows
        ],
        parent_keys=list(failure.memory_keys or []),
        evidence_refs=[(int(a), int(b), str(e)) for a, b, e in refs],
        now=now,
    )
    if not rows:
        raise ValueError("memory_chunk_sources retry produced no rows")
    await ch.insert(
        SOURCES_TABLE,
        rows,
        settings={"async_insert": 1, "wait_for_async_insert": 0},
    )


def _session_from_source_resource_id(source_resource_id: str) -> str:
    parts = str(source_resource_id or "").split(":")
    if len(parts) >= 2 and parts[0] == "memory.set_from_messages":
        return parts[1] or "-"
    return "-"


async def _retry_graph_projection(
    failure: ProjectionFailureRow,
    *,
    ch: Any,
) -> None:
    entries = failure.payload.get("entries")
    results = failure.payload.get("results")
    if not isinstance(entries, list) or not isinstance(results, list):
        raise ValueError("graph projection retry requires entries/results payload")

    from server.embedding import get_embedder
    from server.memory.app.projections import (
        write_entity_edges,
        write_entity_triples,
        write_entity_vectors,
    )

    bulk_input = MemoryBulkSetInput(
        org=failure.org,
        namespace=failure.namespace,
        agent=failure.agent,
        user_id=str(failure.payload.get("user_id") or "-"),
        session_id=str(failure.payload.get("session_id") or "-"),
        run_id=str(failure.payload.get("run_id") or "-"),
        entries=[
            _BulkSetEntry(**entry)
            for entry in entries
            if isinstance(entry, dict) and entry.get("memory_key")
        ],
    )
    bulk_results = [
        _BulkSetResult(**result)
        for result in results
        if isinstance(result, dict) and result.get("memory_key")
    ]
    if not bulk_input.entries or not bulk_results:
        raise ValueError("graph projection retry has no valid entries/results")
    if failure.projection == "entity_vectors":
        await write_entity_vectors(bulk_input, bulk_results, get_embedder(), ch=ch)
    # entity_edges / entity_triples are plain MergeTree, one row per
    # observation: a replay that inserted and then failed to resolve would
    # count its rows again next pass. The failure's id is the dedup token.
    elif failure.projection == "entity_edges":
        await write_entity_edges(
            bulk_input, bulk_results, ch=ch, dedup_token=f"projection-repair:{failure.failure_id}"
        )
    elif failure.projection == "entity_triples":
        await write_entity_triples(
            bulk_input, bulk_results, ch=ch, dedup_token=f"projection-repair:{failure.failure_id}"
        )
