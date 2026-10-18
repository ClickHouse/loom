"""The memory.get answer for one stored row. memory.get and memory.bulk_get both build it
here, so a bulk read serves the same fields from the same columns as a point read."""

from __future__ import annotations

from typing import Any

from server.identity.app.request_scope import request_narrowed
from server.memory.api.models_crud import MemoryGetOutput
from server.memory.domain import lock as _lock
from server.memory.domain import trust as _trust
from server.memory.domain.entity_sentinels import public_entities


def row_output(
    row: Any,
    *,
    sha: str,
    canonical_key: str | None,
    duplicate_of: str,
    superseded_by: str,
    **extra: Any,
) -> MemoryGetOutput:
    """`extra` carries what only the caller computes (memory.get's code links, recall
    breakdown and conflict verdict)."""
    return MemoryGetOutput(
        found=True,
        canonical_key=canonical_key,
        memory_id=str(row.memory_id),
        value=row.value,
        memory_type=row.memory_type,
        confidence=row.confidence,
        version=row.version,
        created_at=row.created_at,
        updated_at=getattr(row, "updated_at", None) or row.created_at,
        expires_at=row.expires_at,
        on_expire=getattr(row, "on_expire", "") or "",
        expire_reason=getattr(row, "expire_reason", "") or "",
        valid_at=getattr(row, "valid_at", None),
        valid_to=getattr(row, "valid_to", None),
        temporal_anchor=getattr(row, "temporal_anchor", None),
        temporal_confidence=float(getattr(row, "temporal_confidence", 0.0) or 0.0),
        char_limit=getattr(row, "char_limit", None),
        pinned=row.pinned,
        sensitivity=str(getattr(row, "sensitivity", "") or ""),
        agent=getattr(row, "agent", "") or "",
        status=getattr(row, "status", None),
        superseded_by=superseded_by,
        duplicate_of=duplicate_of,
        content_sha256=sha,
        tags=list(getattr(row, "tags", None) or []),
        entities=public_entities(getattr(row, "entities", None)),
        recall_count=getattr(row, "recall_count", 0) or 0,
        last_recalled_at=getattr(row, "last_recalled_at", None),
        verified_by=getattr(row, "verified_by", "") or "",
        actor=getattr(row, "actor", "") or "",
        source_class=getattr(row, "source_class", "") or "",
        **_lock.lock_state(row),
        verified_at=getattr(row, "verified_at", None),
        verification=_trust.verification_state(
            getattr(row, "verified_by", ""), getattr(row, "verified_sha256", ""), sha
        ),
        **extra,
    )


def duplicate_of_shown(row: Any, canonical_key: str | None) -> str:
    """The row's `duplicate_of` id as memory.get shows it. A read bounded to one
    user or session sees it only when the canonical resolved inside that scope:
    otherwise the id may name another user's row, and gone and foreign look alike."""
    dup_of = str(getattr(row, "duplicate_of", "") or "")
    if dup_of and not canonical_key and request_narrowed():
        return ""
    return dup_of
