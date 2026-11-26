"""Read current evidence for the exact fact revisions served by memory.search."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.core.hashing import hash_text
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import row_in_read_scope
from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.code_link_reads import guidance_code_states
from server.memory.app.search.pg_validation import doc_key, doc_version
from server.memory.app.use_guidance import guidance_source_states
from server.memory.domain.code_change import worst
from server.memory.domain.redaction_lineage import is_content_hash
from server.memory.domain.search_types import SHARED_AGENT, is_shared_row
from server.memory.domain.temporal_sql import served_statuses
from server.memory.domain.use_guidance import guidance_for_row
from server.memory.store.reads import guidance_heads
from server.memory.store.rows import IdentityKey
from server.memory.store.shared import guidance_heads as shared_guidance_heads
from server.observability import degradations

log = logging.getLogger(__name__)


def _fact_row(hit: _MemorySearchHit) -> bool:
    return hit.source in ("fact", "anchor")


def _status_kept(
    status: str, hit: _MemorySearchHit, input: MemorySearchInput, *, parked: bool
) -> bool:
    if parked and hit.status and hit.status == status:
        # Paging retains the original call's explicit diagnostic inclusion.
        return status in served_statuses(
            include_duplicates=True, include_superseded=True, include_proposed=True
        )
    return status in served_statuses(
        include_duplicates=input.include_duplicates,
        include_superseded=input.include_superseded,
        include_proposed=input.include_proposed,
    )


def _current(row: dict[str, Any], now: datetime) -> bool:
    if row.get("tombstoned_at") is not None or row.get("sensitivity") == "high":
        return False
    for field in ("expires_at", "valid_to"):
        if (value := row.get(field)) is not None:
            until = parse_iso_dt(value)
            if until is None or until <= now:
                return False
    return True


def hit_in_read_scope(identity: tuple[str, ...] | list[str] | None) -> bool:
    scope = request_scope()
    if not scope.narrowed:
        return True
    if identity is None or len(identity) != 3:
        return False
    agent, user_id, session_id = identity
    return agent == SHARED_AGENT or row_in_read_scope(scope, user_id, session_id)


async def _read_heads[K](
    query: Coroutine[Any, Any, dict[K, dict[str, Any]]], *, timeout_s: float, parked: bool
) -> dict[K, dict[str, Any]] | None:
    try:
        return await asyncio.wait_for(query, timeout_s)
    except Exception as exc:  # noqa: BLE001 ? unavailable evidence confers no approval
        log.debug("Search guidance heads unavailable: %s", type(exc).__name__)
        if parked:
            degradations.note("memory", "parked page verification unavailable", health=False)
        return None


def _memory_evidence(
    input: MemorySearchInput,
    hits: list[_MemorySearchHit],
    keys: dict[int, IdentityKey],
    rows: dict[IdentityKey, dict[str, Any]] | None,
    *,
    replay: bool,
    parked: bool,
) -> tuple[set[int], dict[int, dict[str, Any]]]:
    denied: set[int] = set()
    now = naive_utc_now()
    evidence: dict[int, dict[str, Any]] = {}
    if rows is None:
        return denied, evidence
    for i, key in keys.items():
        row = rows.get(key)
        hit = hits[i]
        if (
            row is None
            or not _current(row, now)
            or row.get("redacted_at") is not None
            or (
                not replay
                and not _status_kept(str(row.get("status") or ""), hit, input, parked=parked)
            )
        ):
            denied.add(i)
            continue
        if int(row.get("version") or 0) == doc_version(hit):
            digest = hash_text(row.get("content_sha256"))
            if hit._guidance_content_sha256 and digest != hit._guidance_content_sha256:
                denied.add(i)
                continue
            evidence[i] = dict(row, namespace=key[1], user_id=key[3])
            if not parked and hit._row_chunk is None and is_content_hash(digest):
                hit._guidance_content_sha256 = digest

    return denied, evidence


def _shared_evidence(
    hits: list[_MemorySearchHit],
    keys: dict[int, tuple[str, str]],
    rows: dict[tuple[str, str], dict[str, Any]] | None,
) -> tuple[set[int], dict[int, dict[str, Any]]]:
    denied: set[int] = set()
    evidence: dict[int, dict[str, Any]] = {}
    if rows is None:
        return denied, evidence
    for i, key in keys.items():
        row = rows.get(key)
        if row is None or row.get("tombstoned_at") is not None or row.get("superseded_by"):
            denied.add(i)
        elif int(row.get("version") or 0) == doc_version(hits[i]):
            evidence[i] = {"status": "active"}
    return denied, evidence


async def _evidence_states(
    query: Coroutine[Any, Any, list[str]],
    evidence: dict[int, dict[str, Any]],
    timeout_s: float,
    kind: str,
) -> dict[int, str]:
    try:
        states = await asyncio.wait_for(query, timeout_s)
        return dict(zip(evidence, states, strict=True))
    except Exception as exc:  # noqa: BLE001 ? missing evidence is reported as unknown
        log.debug("Search guidance %s state unavailable: %s", kind, type(exc).__name__)
        return {}


async def attach_search_guidance(
    input: MemorySearchInput,
    hits: list[_MemorySearchHit],
    *,
    parked: bool = False,
    replay: bool = False,
    timeout_s: float = 0.2,
    snapshot_only: bool = False,
) -> list[_MemorySearchHit]:
    """Project authorization-scoped evidence for the served window.

    Parked fact bodies are retained only when the current head still serves that revision.
    A historical read never inherits live review.
    The caller has already checked namespace membership and governed definitions.
    """
    denied = {
        i
        for i, hit in enumerate(hits)
        if (parked or hit._row_identity is not None) and not hit_in_read_scope(hit._row_identity)
    }
    keys: dict[int, IdentityKey] = {
        i: (input.org, *doc_key(hit, input.namespace))
        for i, hit in enumerate(hits)
        if _fact_row(hit)
        and not is_shared_row(hit)
        and hit._row_identity is not None
        and doc_version(hit) > 0
        and i not in denied
    }
    shared_keys = {
        i: (hit.namespace or input.namespace, hit.memory_key)
        for i, hit in enumerate(hits)
        if _fact_row(hit) and is_shared_row(hit) and not hit.governed and doc_version(hit) > 0
    }
    rows: dict[IdentityKey, dict[str, Any]] | None = None
    shared: dict[tuple[str, str], dict[str, Any]] | None = None
    if (not replay or parked) and (keys or shared_keys):
        rows, shared = await asyncio.gather(
            _read_heads(
                guidance_heads(list(set(keys.values()))), timeout_s=timeout_s, parked=parked
            ),
            _read_heads(
                shared_guidance_heads(input.org, list(set(shared_keys.values()))),
                timeout_s=timeout_s,
                parked=parked,
            ),
        )
    denied_memory, evidence = _memory_evidence(
        input, hits, keys, rows, replay=replay, parked=parked
    )
    denied_shared, shared_evidence = _shared_evidence(hits, shared_keys, shared)
    denied |= denied_memory | denied_shared
    evidence.update(shared_evidence)
    authorized = evidence.keys()
    if snapshot_only:
        return [hit for i, hit in enumerate(hits) if i not in denied]
    usable = {
        i: row
        for i, row in evidence.items()
        if not replay
        and not hits[i]._chunk_typed
        and hits[i]._row_chunk is None
        and (not parked or hits[i]._guidance_content_sha256)
    }
    code_states: dict[int, str] = {}
    source_states: dict[int, str] = {}
    if usable:
        eligible = list(usable.values())
        code_states, source_states = await asyncio.gather(
            _evidence_states(guidance_code_states(input.org, eligible), usable, timeout_s, "code"),
            _evidence_states(
                guidance_source_states(input.org, eligible), usable, timeout_s, "source"
            ),
        )

    kept = []
    for i, hit in enumerate(hits):
        if i in denied or (parked and _fact_row(hit) and not hit.governed and i not in authorized):
            continue
        if _fact_row(hit):
            row = usable.get(i, {"status": hit.status, "source_class": hit.source_class})
            hit.use_guidance = guidance_for_row(
                row,
                code_state=code_states.get(i, "unknown"),
                source_state=source_states.get(i, "unknown"),
                procedure_status=(
                    "unknown" if hit.memory_type == "procedural" else "not_applicable"
                ),
            )
            if parked:
                # Link state can change while the served memory revision stays the same.
                hit.code_state = worst([hit.use_guidance.freshness.code])
        kept.append(hit)
    return kept
