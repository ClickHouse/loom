"""memory.apply's wire models: the target a wide write names, each action's
spec, and the per-row result. The resolution and execution live in
server/memory/app/apply.py."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    Field,
    model_validator,
)

from server.memory.api.models_common import _Identity
from server.memory.domain.expire_reasons import caller_reason

MAX_KEYS = 500


class ApplyFilter(BaseModel):
    """Predicates over the identity's live rows. All given predicates AND."""

    memory_type: str | None = None
    tags_any: list[str] = Field(default_factory=list, max_length=50)
    tags_all: list[str] = Field(default_factory=list, max_length=50)
    key_prefix: str | None = Field(default=None, max_length=256)
    created_before: datetime | None = None
    created_after: datetime | None = None
    confidence_lte: float | None = Field(default=None, ge=0.0, le=1.0)
    source_trace: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def _at_least_one(self) -> ApplyFilter:
        if not any(
            v not in (None, [], "")
            for v in (
                self.memory_type,
                self.tags_any,
                self.tags_all,
                self.key_prefix,
                self.created_before,
                self.created_after,
                self.confidence_lte,
                self.source_trace,
            )
        ):
            raise ValueError(
                "filter needs at least one predicate (an empty filter is `all`; use memory.clear)"
            )
        return self


class ApplySearch(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)


class ApplyTarget(BaseModel):
    """Exactly one of keys | filter | search."""

    keys: list[str] | None = Field(default=None, min_length=1, max_length=MAX_KEYS)
    filter: ApplyFilter | None = None
    search: ApplySearch | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ApplyTarget:
        given = [
            n
            for n, v in (("keys", self.keys), ("filter", self.filter), ("search", self.search))
            if v
        ]
        if len(given) != 1:
            raise ValueError(
                f"target must name exactly one of keys | filter | search (got {given or 'none'})"
            )
        return self

    @property
    def kind(self) -> str:
        return "keys" if self.keys else ("filter" if self.filter else "search")


class ApplyExpire(BaseModel):
    ttl_seconds: int | None = Field(default=None, ge=1)
    until: datetime | None = None
    on_expire: Literal["", "tombstone", "demote", "anonymize"] = ""

    @model_validator(mode="after")
    def _one_of(self) -> ApplyExpire:
        if (self.ttl_seconds is None) == (self.until is None):
            raise ValueError("expire needs exactly one of ttl_seconds | until")
        return self


class ApplyLock(BaseModel):
    mode: Literal["read_only", "no_delete"] = "read_only"
    until: datetime | None = None
    ttl_seconds: int | None = Field(default=None, ge=1)


class ApplyLabel(BaseModel):
    tags: list[str] = Field(..., min_length=1, max_length=50)
    mode: Literal["add", "replace", "remove"] = "add"


class ApplyDemote(BaseModel):
    """Exactly one of `confidence_delta` (added, usually negative) or
    `confidence_factor` (multiplied). The result is clamped to [floor, 1]."""

    confidence_delta: float | None = Field(default=None, ge=-1.0, le=1.0)
    confidence_factor: float | None = Field(default=None, gt=0.0, le=1.0)
    floor: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _one_of(self) -> ApplyDemote:
        if (self.confidence_delta is None) == (self.confidence_factor is None):
            raise ValueError("demote needs exactly one of confidence_delta | confidence_factor")
        return self


class ApplyRow(BaseModel):
    memory_key: str
    memory_id: str = ""
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    status: Literal["would_apply", "applied", "refused", "noop", "failed"]
    version: int | None = None
    detail: str = ""


class MemoryApplyInput(_Identity):
    """One scope grammar for every write that can touch more than one row.
    `target` names exactly one of keys | filter | search;
    filter and search need `limit`; `dry_run` is True by default and the
    enforcing call says dry_run=false; `reason` is required and carried
    where a column exists (lock_reason, expire_reason). Every agent at once
    (`agent="*"`) is refused: name one agent (memory.clear tombstones a user's
    rows under every agent).
    """

    # Which rows: exactly one of `keys` (up to 500), `filter` (predicates, ANDed)
    # or `search` (a query, optional `min_score`).
    target: ApplyTarget
    # What to do to each row; every action but `delete` needs its block below.
    action: Literal["delete", "expire", "lock", "label", "demote"]
    # Why (1..512 chars, and for `expire` not starting with `blacklist.sweep:`); stored as
    # lock_reason / expire_reason where the action has such a column.
    reason: str = Field(..., min_length=1, max_length=512)
    # Row cap for a `filter` or `search` target, where it is required; unused for
    # `keys`.
    limit: int | None = Field(default=None, ge=1, le=1000)
    # True (the default) reports `would_apply` rows without writing; false writes.
    dry_run: bool = True
    # For `expire`: `ttl_seconds` or `until`, and an optional `on_expire`.
    expire: ApplyExpire | None = None
    # For `lock`: `mode` (`read_only` / `no_delete`), optional `until` / `ttl_seconds`.
    lock: ApplyLock | None = None
    # For `label`: the `tags` and the `mode` — `add`, `replace` or `remove`.
    label: ApplyLabel | None = None
    # For `demote`: `confidence_delta` or `confidence_factor`; the result is clamped
    # to [`floor`, 1].
    demote: ApplyDemote | None = None

    @model_validator(mode="after")
    def _shape(self) -> MemoryApplyInput:
        if self.agent == "*":
            raise ValueError(
                "memory.apply is per agent: name one agent "
                "(memory.clear tombstones a user's rows under every agent)"
            )
        if self.target.kind != "keys" and self.limit is None:
            raise ValueError("a filter or search target needs `limit`")
        need = {
            "expire": self.expire,
            "lock": self.lock,
            "label": self.label,
            "demote": self.demote,
        }
        if self.action in need and need[self.action] is None:
            raise ValueError(f"action {self.action!r} needs its `{self.action}` block")
        if self.action == "expire":
            caller_reason(self.reason, "reason")  # stored as the rows' expire_reason
        return self


class MemoryApplyOutput(BaseModel):
    # Echo of the request's action.
    action: str
    # Echo of the request's `dry_run`.
    dry_run: bool
    target: str  # keys | filter | search
    # Rows the target resolved to; a key with no live row is not counted.
    matched: int = 0
    applied: int = 0  # rows written (0 under dry_run)
    would_apply: int = 0  # rows that would be written (dry_run)
    refused: int = 0  # locks, policy, preconditions
    # Rows already in the asked state: nothing was written for them.
    noop: int = 0
    # Rows whose write raised; each row's `detail` says why.
    failed: int = 0
    truncated: bool = False  # more rows matched than `limit`
    # One entry per matched row: its identity, status, new version and detail.
    rows: list[ApplyRow] = Field(default_factory=list)
