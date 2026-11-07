"""memory.lock / memory.unlock: a writer-set lock on a key (read-only or
no-delete), until a time or for a TTL."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
)

from server.memory.api.models_common import _Identity


class MemoryLockInput(_Identity):
    """Lock one row against a class of writes.

    `read_only` refuses a changed value, a changed expiry and deletion;
    `no_delete` lets the value change but refuses tombstone, redact, clear,
    consolidate-away and reject. A WRITER may lock, and may re-lock only to a
    stricter-or-equal mode with a later-or-open expiry; loosening is
    `memory.unlock` (ADMIN). `until` / `ttl_seconds` bound the lock (omit
    both for an open-ended lock). Under auth `locked_by` is the caller's
    token subject; the body value is read only when no auth context exists.
    """

    # The key to lock, under this exact identity (a miss answers `locked: false`).
    memory_key: str
    # `read_only` refuses a changed value, a changed expiry and deletion; `no_delete`
    # lets the value change but refuses tombstone, redact, clear, consolidation and reject.
    mode: Literal["read_only", "no_delete"] = "read_only"
    # Why the row is locked (1..512 chars); a refused write's 423 quotes it.
    reason: str = Field(..., min_length=1, max_length=512)
    # When the lock lapses (must be in the future); None and no `ttl_seconds` =
    # open-ended.
    until: datetime | None = None
    # Lock for this many seconds from now; wins over `until` when both are sent.
    ttl_seconds: int | None = Field(default=None, ge=1)
    # Locker recorded when the server runs without auth; under auth the token
    # subject is recorded instead.
    locked_by: str = ""


class MemoryLockOutput(BaseModel):
    locked: bool  # False when the key doesn't exist (or is tombstoned)
    # Why `locked` is False — "not_found" (no live row at this
    # key: never written, tombstoned, or merged into another key). A 200 with
    # locked:false is not a lock.
    reason: str = ""
    # Stable id of the locked row; '' when not found.
    memory_id: str = ""
    # Nanosecond version of the lock row written; None when not found.
    version: int | None = None
    # The mode now in force (`read_only` / `no_delete`); '' when not found.
    lock_mode: str = ""
    # When the lock lapses; None = open-ended, or not found.
    lock_until: datetime | None = None
    # Who holds the lock (`human:<subject>` / `process:<subject>` under auth); '' when not found.
    locked_by: str = ""
    previous_mode: str = ""  # '' when the row was unlocked before this call


class MemoryUnlockInput(_Identity):
    # The key to unlock, under this exact identity.
    memory_key: str
    # Actor reported when the server runs without auth; under auth the token
    # subject is reported instead.
    unlocked_by: str = ""


class MemoryUnlockOutput(BaseModel):
    unlocked: bool  # False when the key doesn't exist or carried no lock
    reason: str = ""  # "not_found" | "not_locked" when unlocked is False
    # Stable id of the row; '' when not found.
    memory_id: str = ""
    # Nanosecond version of the unlocked row; None when nothing was written.
    version: int | None = None
    # The lock mode lifted (a lapsed lock's too); '' when none.
    previous_mode: str = ""
    # Who lifted the lock (`human:<subject>` / `process:<subject>` under auth); '' when
    # nothing was unlocked.
    unlocked_by: str = ""
