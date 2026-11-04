"""The memory.get answer for one stored row. memory.get and memory.bulk_get both build it
here, so a bulk read serves the same fields from the same columns as a point read."""

from __future__ import annotations

from typing import Any

from server.identity.app.request_scope import request_narrowed
from server.memory.api.models_crud import MemoryGetOutput
from server.memory.domain import lock as _lock
from server.memory.domain import trust as _trust
from server.memory.domain.entity_sentinels import public_entities
from server.memory.store.rows import MemoryRow


def row_output(
    row: MemoryRow,
    *,
    sha: str,
    canonical_key: str | None,
    duplicate_of: str,
    superseded_by: str,
    **extra: Any,
) -> MemoryGetOutput:
    """`extra` carries what only memory.get computes (code links, `resolved_from`, recall
    breakdown, conflict verdict)."""
    return MemoryGetOutput(
        found=True,
        canonical_key=canonical_key,
        memory_id=str(row.memory_id),
        value=row.value,
        memory_type=row.memory_type,
        confidence=row.confidence,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at or row.created_at,
        expires_at=row.expires_at,
        on_expire=row.on_expire,
        expire_reason=row.expire_reason,
        valid_at=row.valid_at,
        valid_to=row.valid_to,
        temporal_anchor=row.temporal_anchor,
        temporal_confidence=row.temporal_confidence,
        char_limit=row.char_limit,
        pinned=row.pinned,
        sensitivity=row.sensitivity,
        agent=row.agent,
        user_id=row.user_id,
        session_id=row.session_id,
        status=row.status,
        superseded_by=superseded_by,
        duplicate_of=duplicate_of,
        content_sha256=sha,
        tags=list(row.tags),
        entities=public_entities(row.entities),
        recall_count=row.recall_count,
        last_recalled_at=row.last_recalled_at,
        verified_by=row.verified_by,
        actor=row.actor,
        source_class=row.source_class,
        source_trace=row.source_trace,
        source_resource_id=row.source_resource_id,
        **_lock.lock_state(row),
        verified_at=row.verified_at,
        verification=_trust.verification_state(row.verified_by, row.verified_sha256, sha),
        **extra,
    )


def duplicate_of_shown(row: MemoryRow, canonical_key: str | None) -> str:
    """The row's `duplicate_of` id as memory.get shows it. A read bounded to one
    user or session sees it only when the canonical resolved inside that scope:
    otherwise the id may name another user's row, and gone and foreign look alike."""
    dup_of = row.duplicate_of
    if dup_of and not canonical_key and request_narrowed():
        return ""
    return dup_of
