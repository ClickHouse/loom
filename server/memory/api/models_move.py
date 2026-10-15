"""memory.move — rename a key and leave the old key readable as a redirect.

A move is a duplicate the caller declared: the destination is written through
the real `memory.set` path, and the source row gains a version with
`status='duplicate'` and `duplicate_of` naming the destination — the shape a
hash-collapsed duplicate already has, so `memory.get`
answers the old key with `canonical_key`, `follow: true` serves the new row,
and search / list hide the old one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity


class MemoryMoveInput(_Identity):
    """Rename `from_key` to `to_key`, leaving the old key answering.

    The destination row takes the source's value, type, tags, entities,
    confidence, validity and provenance pointer unless an override below says
    otherwise; the source row becomes a `status='duplicate'` redirect to it
    (`keep_redirect`, default) or is tombstoned. A source that is already a
    redirect answers 409 `ALREADY_REDIRECTED`; one with a change scheduled for later
    (a `memory.amend` transition or a future `valid_at`) answers 409
    `SCHEDULED_CHANGE` — move it once the change applies, or cancel the plan first
    with `memory.amend change_kind='correct'`, the current value and no bounds.
    """

    from_key: str = Field(..., description="The key to rename.")
    to_key: str = Field(..., description="The key the row moves to.")
    # Overrides applied to the NEW row only; the source row is never rewritten.
    value: Any = Field(
        default=None,
        description="Rewrite the value on the destination (e.g. the claim-first "
        "text that replaces a JSON blob). Default: the source's value.",
    )
    memory_type: str | None = Field(default=None, description="Default: the source's.")
    tags: list[str] | None = Field(default=None, description="Default: the source's.")
    entities: list[str] | None = Field(default=None, description="Default: the source's.")
    valid_at: datetime | None = Field(default=None, description="Default: the source's.")
    source_resource_id: str = Field(default="", description="Default: the source's.")
    keep_redirect: bool = Field(
        default=True,
        description="true: the old key stays readable (`memory.get(from_key)` "
        "returns the old row with `canonical_key = to_key`; `follow: true` "
        "serves the new row). false: the old key is tombstoned instead.",
    )
    if_match_sha256: str = Field(
        default="",
        description="Precondition on the SOURCE row's content hash (409 "
        "PRECONDITION_FAILED on mismatch), the memory.set semantics.",
    )
    dry_run: bool = Field(
        default=False,
        description="Report what would happen — destination key, type, value "
        "preview, redirect or tombstone — without writing.",
    )


class MemoryMoveOutput(BaseModel):
    # Echo of the source key.
    from_key: str
    # Echo of the destination key.
    to_key: str
    # True when the source row now redirects to the destination; False when it
    # was tombstoned (`keep_redirect: false`) or nothing was written (dry run).
    redirected: bool
    # The destination row (empty / 0 on a dry run).
    memory_id: str = ""
    # The destination write's version, as `memory.set` returned it.
    version: int = 0
    # SHA-256 of the destination value, as `memory.set` returned it.
    content_sha256: str = ""
    # memory.set's advisories on the destination write, passed through.
    advisories: list[str] = Field(default_factory=list)
    # Echo of the request's `dry_run`.
    dry_run: bool = False
    # Dry-run preview of the destination: its type and the first 200 chars.
    memory_type: str = ""
    # Dry run only: the first 200 chars of the destination value.
    value_preview: str = ""
