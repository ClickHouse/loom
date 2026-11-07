"""memory.lock / memory.unlock — a write-time invariant on one row.

Locking is a WRITER action and only ever tightens: a locked row accepts a
re-lock to a stricter-or-equal mode with a later-or-open expiry (and a new
reason), and refuses anything looser with the same 423 the guarded writes
raise. Loosening is `memory.unlock`, an ADMIN action.

Both write a new ReplacingMergeTree version row carrying every live column
forward (build_memory_version_row), so a lock is history like any other
edit: memory.history shows who locked what, when, and why.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import HTTPException

from server.auth.context import current_auth
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now, to_naive_utc
from server.identity.app.request_scope import request_scope
from server.memory.api.models_lock import (
    MemoryLockInput,
    MemoryLockOutput,
    MemoryUnlockInput,
    MemoryUnlockOutput,
)
from server.memory.domain import lock as _lock
from server.memory.domain import trust as _trust
from server.memory.store.inserts import insert_memory_versions
from server.memory.store.rows import next_version

if TYPE_CHECKING:
    from server.memory.store import MemoryRow


def _actor(requested: str) -> str:
    auth = current_auth()
    if auth is not None and auth.subject:
        return _trust.actor_for(auth.subject, is_service_account=bool(auth.is_service_account))
    return (requested or "").strip()[:128]


async def _head(input: MemoryLockInput | MemoryUnlockInput) -> MemoryRow | None:
    import server.memory.store as memory_repo

    return await memory_repo.get_by_key(
        memory_repo.Identity(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            session_id=input.session_id,
        ),
        input.memory_key,
        fresh=True,  # a lock follows a set; a replica-lag miss is not 'not found'
        strong=True,  # a lock built on a stale head erases a newer write
        scope=request_scope(),
    )


async def handle_memory_lock(input: MemoryLockInput) -> MemoryLockOutput:
    import server.memory.store as memory_repo

    existing = await _head(input)
    if existing is None:
        return MemoryLockOutput(locked=False, reason="not_found")
    now = naive_utc_now()
    until: datetime | None
    if input.ttl_seconds is not None:
        until = now + timedelta(seconds=input.ttl_seconds)
    else:
        until = to_naive_utc(input.until)
    if until is not None and until <= now:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION", "message": "lock `until` must be in the future"},
        )
    previous = _lock.effective_mode(existing, now=now)
    if previous:
        prev_until = to_naive_utc(getattr(existing, "lock_until", None))
        loosens_mode = not _lock.is_stricter_or_equal(input.mode, previous)
        # An open-ended lock (NULL) is the longest; a shorter `until` loosens.
        loosens_until = (until is not None) and (prev_until is None or until < prev_until)
        if loosens_mode or loosens_until:
            exc = _lock.refusal(existing, "loosen", op="memory.lock", mode=previous)
            exc.detail["message"] = (  # type: ignore[index]
                f"memory.lock refused: {input.memory_key!r} is already locked "
                f"{previous}; a WRITER may only tighten a lock. "
                "Only the user can loosen it; "
                "ask them rather than looking for another route or credential."
            )
            raise exc
    version = next_version()
    row = memory_repo.build_memory_version_row(
        existing,
        version=version,
        updated_at=memory_repo.written_now(),
        lock_mode=input.mode,
        lock_reason=input.reason.strip()[:512],
        lock_until=until,
        locked_by=_actor(input.locked_by),
        locked_at=now,
    )
    await insert_memory_versions([row], ch=get_client())
    return MemoryLockOutput(
        locked=True,
        memory_id=str(existing.memory_id),
        version=version,
        lock_mode=input.mode,
        lock_until=until,
        locked_by=row.get("locked_by", "") or "",
        previous_mode=previous,
    )


async def handle_memory_unlock(input: MemoryUnlockInput) -> MemoryUnlockOutput:
    import server.memory.store as memory_repo

    existing = await _head(input)
    if existing is None:
        return MemoryUnlockOutput(unlocked=False, reason="not_found")
    previous = _lock.effective_mode(existing)
    if not previous and not (getattr(existing, "lock_mode", "") or ""):
        # Nothing to lift: idempotent no-op, no version row.
        return MemoryUnlockOutput(
            unlocked=False,
            memory_id=str(existing.memory_id),
            previous_mode="",
            reason="not_locked",
        )
    version = next_version()
    row = memory_repo.build_memory_version_row(
        existing,
        version=version,
        updated_at=memory_repo.written_now(),
        lock_mode="",
        lock_reason="",
        lock_until=None,
        locked_by="",
        locked_at=None,
    )
    await insert_memory_versions([row], ch=get_client())
    return MemoryUnlockOutput(
        unlocked=True,
        memory_id=str(existing.memory_id),
        version=version,
        previous_mode=previous or (getattr(existing, "lock_mode", "") or ""),
        unlocked_by=_actor(input.unlocked_by),
    )
