"""Audit plane — query `loom.audit_log` scoped to (org, namespace).

The audit log carries a per-row SHA-256 tamper-evident hash (NOT a
hash chain — see `server/observability/audit.py` header for the
rationale). This op surfaces rows for the caller's tenancy with
optional filtering and keyset pagination. Hash re-verification runs
in the `audit_verifier` worker, not here.

Required role: VIEWER — the handler self-scopes (platform/org admins
see org-wide; everyone else only their accessible namespaces).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from server.operations.read_projection import ProjectedRow

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 50


class AuditListInput(BaseModel):
    org: str
    # None: every namespace the caller may read; naming one it cannot read gives an empty page.
    namespace: str | None = None
    event_type: str | None = None  # exact match
    since: datetime | None = None  # only rows ts >= since
    # At most this many rows per page, newest first; 1 to 500.
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT)
    cursor: str | None = None  # opaque token from prior page
    # The output fields the caller reads (a whitelist
    # resolved against the row model; an unknown name is a 422); unset, every
    # field. Every list operation is projectable.
    fields: list[str] | None = None


class AuditListRow(ProjectedRow):
    ts: datetime
    event_type: str
    org: str
    namespace: str
    actor: str
    # JSON string; caller decides whether to deserialize. An `erasure.failed` `error` or
    # `erasure.abandoned` `last_error` an older build wrote with the driver's text is
    # cut to the step and the error's kind; `row_hash` covers the stored subject, not
    # the returned one.
    subject: str
    request_id: str
    row_hash: str


class AuditListOutput(BaseModel):
    # The page of audit rows, newest first (ties broken by `request_id`).
    rows: list[AuditListRow]
    # Pass back as `cursor` for the next, older page; None when this page is the last.
    next_cursor: str | None = None
