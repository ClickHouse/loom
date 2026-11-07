"""Conflicts surface — list + resolve + allowlist management.

memory.set flags semantically-close-but-different rows `status='conflicted'`
and cross-links them via `conflict_with`. `resolve` decisions:
  - revise:   chosen memory wins; peer(s) contracted
  - contract: chosen memory is wrong; tombstone it, restore peer(s)
  - allow:    both legitimate; reactivate both + allowlist the pair
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from server.operations.base import MCP_HIDDEN
from server.operations.read_projection import ProjectedRow

ResolveDecision = Literal["revise", "contract", "allow"]


class ConflictsListInput(BaseModel):
    org: str
    namespace: str
    # At most this many rows, 1-500, newest version first.
    limit: int = Field(default=50, ge=1, le=500)
    # Keyset cursor — pass previous response's earliest version to paginate.
    before_version: int | None = None
    # A badge needs the number, not the rows. With
    # count_only the answer is `total` and no row (no value column read).
    count_only: bool = False
    # With count_only, count now instead of reading the minute-long cache; default off.
    fresh: bool = Field(default=False, json_schema_extra=MCP_HIDDEN)
    # The Memories view marks cards that have a
    # conflict and needs only the keys. With keys_only the answer is `keys`
    # from one FINAL read of the key columns — no value column.
    keys_only: bool = False
    # The output fields the caller reads (a whitelist resolved against the row model; an
    # unknown name is a 422); unset, every field. Every row keeps `memory_key` and
    # `sensitivity`. Every list operation is projectable.
    fields: list[str] | None = None


class _ConflictRow(ProjectedRow):
    memory_id: str
    memory_key: str
    memory_type: str
    # The row's value; null for a high row, whose value is read by key (memory.get).
    value: dict[Any, Any] | list[Any] | str | int | float | bool | None
    version: int
    pinned: bool
    created_at: datetime
    conflict_with: list[str]
    content_sha256: str = ""  # "" for a high row
    # Sensitivity tier ('' | low | medium | high); a high row comes without its value.
    sensitivity: str = ""


class ConflictsListOutput(BaseModel):
    # Conflicted memories, newest version first; empty on a `count_only` or `keys_only`
    # call.
    rows: list[_ConflictRow]
    # Pass as `before_version` for the next page; null on the last page.
    next_before_version: int | None = None
    # The live conflicted count for the namespace; set on a count_only call.
    total: int | None = None
    # The conflicted memory keys of the namespace; set on a keys_only call.
    keys: list[str] | None = None


class ConflictsResolveInput(BaseModel):
    org: str
    namespace: str
    # The conflicted memory the decision is about (an org-wide id); it must live in
    # `namespace`.
    memory_id: str
    # `revise` keeps it and tombstones the peers; `contract` tombstones it and reactivates
    # the peers; `allow` reactivates all and allowlists each key pair.
    decision: ResolveDecision
    # Optional: scope the resolve to one specific peer (revise against
    # just that one). If unset, the resolve fans out across all peers
    # in conflict_with.
    peer_id: str | None = None
    # Why; appended to tombstoned rows' `source_trace` and stored on allowlist rows
    # (first 512 characters).
    reason: str = ""
    # Who resolved it, recorded beside `reason` (first 128 characters); not checked
    # against the token.
    resolved_by: str = ""


class ConflictsResolveOutput(BaseModel):
    # False when the memory or the named peer is unknown, or the memory is not in
    # `namespace`; nothing was written then. A peer not in `namespace` is left as it is.
    resolved: bool
    # The decision, echoed.
    decision: ResolveDecision
    # Every memory id this call tombstoned or reactivated.
    affected_memory_ids: list[str]
    # When the resolve ran (UTC); null when nothing was resolved.
    resolved_at: datetime | None = None
    # The namespace's conflicted count under the caller's read scope, counted after the
    # writes; null when nothing was resolved or the count failed.
    total: int | None = None


class ConflictsProposeRevisionInput(BaseModel):
    org: str
    namespace: str
    # The conflicted memory to advise on (an org-wide id).
    memory_id: str
    # Limit the advice to this one peer from its `conflict_with`; None considers every
    # peer.
    peer_id: str | None = None


class _ConflictCandidateMemory(BaseModel):
    local_id: int
    memory_id: str
    memory_key: str
    value: dict[Any, Any] | list[Any] | str | int | float | bool | None
    pinned: bool
    created_at: datetime


class _ConflictRevisionSuggestion(BaseModel):
    decision: ResolveDecision
    target_local_id: int
    peer_local_ids: list[int]
    rationale: str
    apply_operation: str = "conflicts.resolve"


class ConflictsProposeRevisionOutput(BaseModel):
    # False when the memory, or every row it names, is not live in `namespace`.
    found: bool
    # The target first (`local_id` 1), then its live peers, newest first.
    memories: list[_ConflictCandidateMemory] = Field(default_factory=list)
    # At most one suggested `conflicts.resolve`, by local id: revise when the target is
    # pinned or shares a peer's key, contract when a peer is pinned, else allow.
    suggestions: list[_ConflictRevisionSuggestion] = Field(default_factory=list)
    # Always says that nothing was applied: this operation only advises.
    note: str = "No changes were applied."


class ConflictsAllowlistListInput(BaseModel):
    org: str
    namespace: str
    # At most this many pairs, 1-1000, newest first.
    limit: int = Field(default=100, ge=1, le=1000)


class _AllowlistRow(BaseModel):
    memory_key_a: str
    memory_key_b: str
    allowed_at: datetime
    allowed_by: str
    reason: str


class ConflictsAllowlistListOutput(BaseModel):
    # Live allowed key pairs (`memory_key_a` sorts before `memory_key_b`), newest first.
    rows: list[_AllowlistRow]


class ConflictsAllowlistRemoveInput(BaseModel):
    """Revoke an allow decision; the pair will re-flag on future writes."""

    org: str
    namespace: str
    # One key of the allowed pair; the order of the two keys does not matter.
    memory_key_a: str
    # The other key of the pair.
    memory_key_b: str
    # Who revoked it; appended with `reason` to the pair's stored reason.
    removed_by: str = ""
    # Why; appended to the pair's stored reason.
    reason: str = ""


class ConflictsAllowlistRemoveOutput(BaseModel):
    # False when no live allowed pair matched.
    removed: bool
