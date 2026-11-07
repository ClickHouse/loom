"""Blacklist wire ops — add / list / remove."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

PatternType = Literal["substring", "regex"]
Scope = Literal["namespace", "org", "global"]


class BlacklistAddInput(BaseModel):
    org: str
    namespace: str
    # The text to refuse, matched case-sensitively against a stored value: as a
    # substring, or as a regex of at most 256 characters.
    pattern: str = Field(..., min_length=1, max_length=4096)
    # `substring` (plain text) or `regex` (RE2, on writes and in the sweep; bounded at add).
    pattern_type: PatternType = "substring"
    # Reach: `namespace` (this one), `org` (every namespace; needs org admin) or
    # `global` (every tenant; needs platform admin).
    scope: Scope = "namespace"
    # Why the pattern exists; stored and listed.
    reason: str = ""
    # Author recorded only when the call has no authenticated principal to stamp.
    created_by: str = ""
    # Also tombstone the live rows the pattern already matches: inside this org, and
    # inside the namespace for a namespace-scoped pattern. Up to 5,000 rows per call;
    # `swept_truncated` says to call blacklist.sweep again.
    sweep: bool = False


class BlacklistAddOutput(BaseModel):
    # The new pattern's id, for `blacklist.sweep` and `blacklist.remove`.
    pattern_id: str
    # When the pattern was stored (UTC).
    created_at: datetime
    # Rows the sweep tombstoned; 0 without `sweep`.
    swept: int = 0
    # True when more rows matched than one sweep takes; call `blacklist.sweep`.
    swept_truncated: bool = False
    # "" when the sweep finished, else why not (the pattern is stored either way):
    # `<code>: <message>` (rerun with blacklist.sweep) or the failed match query's message.
    sweep_error: str = ""


class BlacklistSweepInput(BaseModel):
    org: str
    namespace: str
    # The live pattern to sweep with; an unknown id or one outside the caller's reach
    # answers 404.
    pattern_id: str


class BlacklistSweepOutput(BaseModel):
    # The pattern swept with, echoed.
    pattern_id: str
    # Live rows the pattern matched in this call: at most 5,000 in ClickHouse, plus,
    # under the Postgres authority, the parked heads this call tombstoned.
    matched: int
    # Rows this call tombstoned.
    tombstoned: int
    # True when more rows matched than one call takes; call again.
    truncated: bool
    # "" when the sweep finished, else `<code>: <message>` once rows may have landed (call
    # again) or the failed match query's message; a failure before that is the call's.
    error: str = ""


class BlacklistListInput(BaseModel):
    org: str
    namespace: str
    # scope=None returns every pattern that applies to this (org, namespace),
    # which includes higher-scoped (org-wide, global) patterns too — the
    # effective rule set, not just the ones literally pinned to this scope.
    scope: Scope | None = None
    # At most this many patterns, 1-1000, newest first.
    limit: int = Field(default=100, ge=1, le=1000)


class _BlacklistRow(BaseModel):
    pattern_id: str
    org: str
    namespace: str
    pattern: str
    pattern_type: PatternType
    scope: Scope
    reason: str
    created_by: str
    hit_count: int
    last_hit_at: datetime | None = None
    created_at: datetime


class BlacklistListOutput(BaseModel):
    # The live patterns that match the scope filter, newest first.
    rows: list[_BlacklistRow]


class BlacklistRemoveInput(BaseModel):
    org: str
    # The pattern to tombstone; a namespace pattern outside the caller's reach
    # answers `removed=false`.
    pattern_id: str
    # Recorded on the blacklist cache's version marker only, not on the pattern row.
    removed_by: str = ""
    # Accepted but not stored.
    reason: str = ""


class BlacklistRemoveOutput(BaseModel):
    # False when no live pattern with that id is visible to the caller.
    removed: bool
    # When the pattern was tombstoned (UTC); null when nothing was removed.
    removed_at: datetime | None = None
