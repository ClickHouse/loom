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
    # The user id, namespace or (for `namespace_list`) a JSON array of 1–500 names or
    # {"namespaces": [...], "purge": bool}; only a platform admin with auth on may name a
    # mirror (`acl-` and 24 hex digits). A list is one saga, one mutation per table.
    scope_value: str
    requester: str = Field(..., description="email or service account that requested the erasure")
    # `normal` or `urgent`; recorded on the saga's ledger row, the saga runs the same either way.
    urgency: Literal["normal", "urgent"] = "normal"
    # Hard-delete the rows of the soft-delete tables (memory, embeddings,
    # chunks, documents, summaries, links) instead of stamping tombstoned_at:
    # a tombstone keeps the bytes in every FINAL read and merge; a purge takes
    # them out of the parts. For a `user_id` scope or throwaway namespaces.
    # No restore window.
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
    # Per-step row counts; empty for a withheld mirror receipt, disclosing no failed stage.
    # A step done by an earlier attempt reads `rows_affected` 0 and a `skipped` note.
    steps: list[_ErasureStepResult]
    # When the saga was registered (UTC).
    requested_at: datetime
    # When the saga reached its terminal state (UTC); `None` while it runs.
    completed_at: datetime | None = None
    # Live residue count; zero for a withheld mirror receipt, so zero alone does not prove absence.
    residue_count: int = 0
    # Nonzero live counts by substrate; `{}` for a withheld mirror receipt does not prove absence.
    # Postgres tables use `pg:<table>`, bucket objects `objects.document_originals`.
    residue: dict[str, int] = Field(default_factory=dict)
    # Census completion time (UTC), or `None` when incomplete, never run or withheld;
    # only status/list to a flat platform admin expose marked evidence.
    verified_at: datetime | None = None
    # Complete census counts including zeros, or `None` when unavailable or withheld.
    census: dict[str, int] | None = None
    # The scope's delete-or-tombstone mode (a single namespace's is tombstone whatever
    # was asked); a scope that holds a mirror is deleted whatever this says.
    purge: bool = False
    # Failure reason, or `""` on success; withheld mirror receipts use `ERASURE_FAILED`
    # for every cause. Ordinary reasons name the step and exception kind or Loom error.
    failure_reason: str = ""
    # Names of the steps finished so far, sorted; empty for a withheld mirror receipt.
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
    # The saga's scope (user id, namespace, or the JSON list); a list leaves out any
    # mirror's name but for a platform admin.
    scope_value: str | None = None
    # Who asked for the erasure, as given in the request.
    requester: str | None = None
    # `in_progress`, `completed` or `failed`; `None` when not found.
    status: ErasureStatus | None = None
    # How many steps have finished; 99 once completed, zero for a withheld mirror receipt.
    current_step: int | None = None
    # When the saga was registered (UTC).
    requested_at: datetime | None = None
    # When the saga reached its terminal state (UTC); `None` while in progress.
    completed_at: datetime | None = None
    # Failure reason, or `None` without a failure; withheld mirror receipts use
    # `ERASURE_FAILED` for every cause. Ordinary reasons name the step, error or recovery limit.
    failure_reason: str | None = None
    # Live residue count; zero for a withheld mirror receipt, so zero alone does not prove absence.
    residue_count: int = 0
    # Nonzero live counts by substrate; `{}` for a withheld mirror receipt does not prove absence.
    # Postgres tables use `pg:<table>`, bucket objects `objects.document_originals`.
    residue: dict[str, int] = Field(default_factory=dict)
    # Census completion time (UTC), or `None` when incomplete, never run or withheld;
    # a flat platform admin retains marked evidence.
    verified_at: datetime | None = None
    # Complete census counts including zeros, or `None` when unavailable or withheld.
    census: dict[str, int] | None = None
    # The scope's delete-or-tombstone mode (a single namespace's is tombstone whatever
    # was asked); a scope that holds a mirror is deleted whatever this says.
    purge: bool = False


# ============================================================
# erasure.export — the rows about a user or namespace, as one dump
# ============================================================


class ErasureExportInput(BaseModel):
    org: str
    # `user_id` exports one user's rows across the org (org admin only); `namespace` exports
    # one namespace (admin on it). Any other value is read as a namespace name.
    scope_type: ScopeType
    # The user id or namespace to export; never a mirror's name (`acl-` and 24 hex digits).
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
    # As `erasure.status` answers it: a list leaves out any mirror's name but for a
    # platform admin.
    scope_value: str
    requester: str
    status: ErasureStatus
    requested_at: datetime
    completed_at: datetime | None = None
    # Why a failed saga failed, `""` otherwise; the shapes `erasure.status` names.
    failure_reason: str = ""
    # Zero for a withheld mirror receipt, regardless of the failed stage.
    steps_completed_count: int = 0
    # Rows tombstoned here but later memory.restore'd; non-zero means
    # "completed" is partially un-true, so the UI surfaces it alongside status.
    reversals_count: int = 0
    # Census residue on the terminal row; zero with `verified_at` None when the
    # mirror receipt is withheld from a reader below flat platform admin.
    residue_count: int = 0
    verified_at: datetime | None = None


class ErasureListOutput(BaseModel):
    # Recent sagas, newest first: org-wide for an org admin, otherwise only
    # `namespace` sagas on namespaces the caller belongs to.
    rows: list[ErasureListRow]
