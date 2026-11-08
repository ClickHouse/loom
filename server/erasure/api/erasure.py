"""Erasure plane wire models: subject-scoped delete and export.

Ops:
  - `erasure.request`  — run a delete saga over one scope
  - `erasure.status`   — a saga's progress and step results
  - `erasure.export`   — the subject's data as JSON (data portability)
  - `erasure.list`     — recent sagas the caller may see

Scopes (`ScopeType`): `user_id` (one user across the org's namespaces),
`namespace` (one namespace) and `namespace_list` (several, one saga).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from server.erasure.domain.scopes import parse_namespace_list

ScopeType = Literal["user_id", "namespace", "namespace_list"]
ErasureStatus = Literal["in_progress", "completed", "failed"]


# ============================================================
# erasure.request
# ============================================================


class ErasureRequestInput(BaseModel):
    org: str
    # What `scope_value` names: `user_id` (one user across the org's namespaces, org admin only),
    # `namespace` (one namespace, admin on it) or `namespace_list` (several, org admin only).
    scope_type: ScopeType
    # For `namespace_list`: a JSON array of namespace names (1–500), or a JSON
    # object {"namespaces": [...], "purge": bool}. One saga, one mutation per
    # table for the whole list.
    scope_value: str
    requester: str = Field(..., description="email or service account that requested the erasure")
    # `normal` or `urgent`; recorded on the saga's ledger row, the saga runs the same either way.
    urgency: Literal["normal", "urgent"] = "normal"
    # Hard-delete the rows of the soft-delete tables (memory, embeddings,
    # chunks, documents, summaries, links) instead of stamping tombstoned_at:
    # a tombstone keeps the bytes in every FINAL read and merge; a purge takes
    # them out of the parts. For a `user_id` scope (a GDPR deletion) or
    # throwaway namespaces. No restore window.
    purge: bool = False
    # External backends (Langfuse traces by tag, ClickStack OTel rows, …) run
    # for every namespace of the scope and fail the saga closed. A
    # `namespace_list` erasure of throwaway namespaces may skip them; a
    # `user_id` or single `namespace` erasure never can (right to be
    # forgotten spans the backends). Kept in the ledger's scope_value like
    # `purge`, so a resumed saga honours it.
    external: bool = True

    @model_validator(mode="after")
    def _list_scope_is_a_json_array(self) -> ErasureRequestInput:
        _check_user_scope(self.scope_type, self.scope_value)
        if self.scope_type == "namespace_list":
            try:
                names = parse_namespace_list(self.scope_value)
            except ValueError as e:
                raise ValueError(f"namespace_list scope_value: {e}") from None
            if not names:
                raise ValueError("namespace_list scope_value names no namespace")
            if len(names) > 500:
                raise ValueError("namespace_list scope_value names more than 500 namespaces")
        elif not self.external:
            raise ValueError("external=false is only allowed for scope_type namespace_list")
        return self


# The user ids no person has: a row written by no end user carries `-` (or, from
# older writers, ''), so a scope of one of them would reach every such row.
_NO_USER = frozenset({"", "-", "*"})


def _check_user_scope(scope_type: str, scope_value: str) -> None:
    if scope_type == "user_id" and scope_value.strip() in _NO_USER:
        raise ValueError(f"user_id scope_value {scope_value!r} names no user")


def _scope_flag(scope_value: str, key: str, default: bool) -> bool:
    """A flag as stored in a `namespace_list` scope_value object, so a resumed
    saga (the recovery worker re-reads the ledger row) keeps the requester's
    choice; `default` for a bare array or a missing key."""
    try:
        parsed = json.loads(scope_value or "")
    except json.JSONDecodeError:
        return default
    if isinstance(parsed, dict) and key in parsed:
        return bool(parsed[key])
    return default


def parse_external_flag(scope_value: str, default: bool = True) -> bool:
    return _scope_flag(scope_value, "external", default)


def parse_purge_flag(scope_value: str, default: bool = False) -> bool:
    return _scope_flag(scope_value, "purge", default)


class _ErasureStepResult(BaseModel):
    step: str
    rows_affected: int
    notes: str = ""


class ErasureRequestOutput(BaseModel):
    # Id of the saga; pass it to `erasure.status`.
    erasure_id: str
    # `completed`, or `failed` when a step or the residue check failed;
    # the recovery worker retries a failed saga.
    status: ErasureStatus
    # One entry per saga step with the rows it touched; a step done by an earlier attempt
    # reads `rows_affected` 0 and a `skipped` note.
    steps: list[_ErasureStepResult]
    # When the saga was registered (UTC).
    requested_at: datetime
    # When the saga reached its terminal state (UTC); `None` while it runs.
    completed_at: datetime | None = None
    # Verified forgetting: live rows and bucket objects the post-erasure
    # census still found in scope, per substrate; 0 / {} on a completed saga.
    residue_count: int = 0
    # Rows still live per table after the saga, only tables with a non-zero count (the
    # Postgres authority's under `pg:<table>`), and the bucket objects left, under the key
    # `objects.document_originals`; `{}` on success.
    residue: dict[str, int] = Field(default_factory=dict)
    # When the residue census ran (UTC); `None` if it never ran or left a store uncounted
    # (the Postgres authority or the bucket, which the server log names); `residue` then
    # holds what it did count.
    verified_at: datetime | None = None
    # Why a failed saga failed, `""` otherwise: `<step>: <error>` or a bare `<error>`
    # outside a step, where `<error>` is the exception's kind or a Loom error's
    # `<CODE>: <message>`, or `verify.residue: ErasureResidueError: residue after
    # erasure: {table: n}`.
    failure_reason: str = ""
    # Names of the steps finished so far, sorted.
    steps_completed: list[str] = Field(default_factory=list)


# ============================================================
# erasure.status
# ============================================================


class ErasureStatusInput(BaseModel):
    # Id returned by `erasure.request`.
    erasure_id: str


class ErasureStatusOutput(BaseModel):
    # False for an unknown id and for a saga the caller may not read (the two look alike).
    found: bool
    # The saga id asked about.
    erasure_id: str
    org: str | None = None
    # The saga's scope kind (`user_id`, `namespace` or `namespace_list`); `None` when not found.
    scope_type: str | None = None
    # The saga's scope as requested (user id, namespace, or the JSON list).
    scope_value: str | None = None
    # Who asked for the erasure, as given in the request.
    requester: str | None = None
    # `in_progress`, `completed` or `failed`; `None` when not found.
    status: ErasureStatus | None = None
    # How many steps have finished; 99 once the saga completed.
    current_step: int | None = None
    # When the saga was registered (UTC).
    requested_at: datetime | None = None
    # When the saga reached its terminal state (UTC); `None` while in progress.
    completed_at: datetime | None = None
    # Why a failed saga failed, `None` otherwise: a reason `erasure.request` names, or
    # `max_attempts (N) exceeded` once recovery gave up, then `; last: <reason>` if any;
    # `the saga failed; the error is in the server log` for a reason in neither shape.
    failure_reason: str | None = None
    # Verified forgetting.
    residue_count: int = 0
    # Rows still live per table at the last census (the Postgres authority's under
    # `pg:<table>`), and the bucket objects left, under the key
    # `objects.document_originals`; `{}` when none.
    residue: dict[str, int] = Field(default_factory=dict)
    # When the last residue census ran (UTC); `None` if it has not run or left a store
    # uncounted (the Postgres authority or the bucket, which the server log names);
    # `residue` then holds what it did count.
    verified_at: datetime | None = None


# ============================================================
# erasure.export — GDPR Article 20 right-to-portability
# ============================================================


class ErasureExportInput(BaseModel):
    org: str
    # `user_id` exports one user's rows across the org (org admin only); `namespace` exports
    # one namespace (admin on it). Any other value is read as a namespace name.
    scope_type: ScopeType
    # The user id or namespace to export.
    scope_value: str
    requester: str = Field(..., description="email or service account requesting the export")
    include_tombstoned: bool = Field(
        default=True,
        description="Include tombstoned memory and conversation rows. Default True: subject "
        "has the right to know what was held about them, even if pending deletion.",
    )
    # Per-section caps. If a section exceeds, `truncated[section]=True` is set.
    # 10k rows is enough for an internal admin tool; ops can re-query CH for the rest.
    section_limit: int = Field(default=10_000, ge=1, le=100_000)

    @model_validator(mode="after")
    def _names_a_user(self) -> ErasureExportInput:
        _check_user_scope(self.scope_type, self.scope_value)
        return self


class ErasureExportSection(BaseModel):
    rows: list[dict[str, object]]
    count: int
    truncated: bool = False


class ErasureExportOutput(BaseModel):
    # When the export was read (UTC).
    exported_at: datetime
    org: str
    # Echo of the requested scope kind.
    scope_type: ScopeType
    # Echo of the requested user id or namespace.
    scope_value: str
    # Echo of who asked for the export.
    requester: str
    # Rows returned per section (`memory`, `knowledge`, `conversations`,
    # `conversation_threads`, `agent_events`, `audit_log`): at most `section_limit`, and
    # the section's `truncated` says more exist.
    counts: dict[str, int]
    # `loom.memory` rows in scope (under the Postgres authority, its heads); tombstoned rows
    # too when `include_tombstoned`.
    memory: ErasureExportSection
    # Knowledge chunk rows of the namespace, or every one the user wrote for a `user_id` scope.
    knowledge: ErasureExportSection
    # Turns and saved messages in scope with their `sensitivity`, up to
    # `erasure_export_conversation_max_bytes` of text plus the turn that crosses it.
    # `is_saved` marks a saved message, 'high' marks text that reads as high, and
    # tombstoned rows come too when `include_tombstoned`.
    conversations: ErasureExportSection
    # Threads in scope with their title, metadata and `sensitivity`, capped by
    # `section_limit` rows only (no byte budget).
    conversation_threads: ErasureExportSection
    # Per-request ledger rows in scope, newest first.
    agent_events: ErasureExportSection
    # Audit rows in scope; for `user_id`, rows whose subject mentions the user id.
    audit_log: ErasureExportSection


class ErasureListInput(BaseModel):
    org: str
    # At most this many sagas, newest first; the server caps it at 500.
    limit: int = Field(default=50, ge=1, le=500)
    status: ErasureStatus | None = None  # filter to one status if needed


class ErasureListRow(BaseModel):
    erasure_id: str
    scope_type: str
    scope_value: str
    requester: str
    status: ErasureStatus
    requested_at: datetime
    completed_at: datetime | None = None
    # Why a failed saga failed, `""` otherwise; the shapes `erasure.status` names.
    failure_reason: str = ""
    steps_completed_count: int = 0
    # Rows tombstoned here but later memory.restore'd; non-zero means
    # "completed" is partially un-true, so the UI surfaces it alongside status.
    reversals_count: int = 0
    # Verified forgetting: census residue on the terminal row.
    residue_count: int = 0
    verified_at: datetime | None = None


class ErasureListOutput(BaseModel):
    # Recent sagas, newest first: org-wide for an org admin, otherwise only
    # `namespace` sagas on namespaces the caller belongs to.
    rows: list[ErasureListRow]
