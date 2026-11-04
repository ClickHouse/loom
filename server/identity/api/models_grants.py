"""Per-namespace RBAC grants — wire models.

The auth path consults `loom.namespace_grants` as a fallback when a
JWT's blanket role can't satisfy an op's `required_role`. This module
models the wire ops for managing the grants themselves:

  - `grants.create`  — issue a grant
  - `grants.list`    — show grants in a namespace
  - `grants.revoke`  — drop a grant
  - `grants.approve` — approve a pending elevation request
  - `grants.share_agent` / `grants.unshare_agent` — an agent lends its own
    namespace to another agent of its trust, or takes it back

The first four are ADMIN-role + audited — only existing admins on an org
can elevate other principals. The agent ops are WRITER-role + audited, and
reach only the caller's own namespace and its trust's agents.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.operations.base import GrantRoleStr
from server.operations.read_projection import ProjectedRow

GrantStatusStr = Literal["active", "pending"]


class GrantsCreateInput(BaseModel):
    org: str
    namespace: str
    # A login handle, an email (it admits an account once that email is verified: an SSO
    # account's or a linked one's), or a service-account `sub` claim.
    subject: str
    # `viewer`, `writer` or `admin`; a code namespace takes only `viewer` (reader) and
    # `admin` (maintainer).
    role: GrantRoleStr
    # Ignored under auth: the server stamps the authenticated caller. Honoured
    # only with auth off (dev), where no caller exists.
    granted_by: str = "admin"
    # The table replaces on (org, namespace, subject), so a
    # create below the subject's current active role would lower it silently.
    # Default refuses that with 409 GRANT_WOULD_DOWNGRADE; `replace=true` is
    # the explicit "yes, lower it". Same-role re-creates and upgrades never
    # need it.
    replace: bool = False


class GrantsCreateOutput(BaseModel):
    org: str
    namespace: str
    # The subject as the request spelled it (the row is stored under the login handle
    # of the account whose verified email it is, else as spelled).
    subject: str
    # The role now active for the subject.
    role: GrantRoleStr
    # When the grant row was written (UTC).
    granted_at: datetime


class GrantsListInput(BaseModel):
    org: str
    namespace: str
    # Rows per page, one per person, newest grant first; 1 to 500.
    limit: int = Field(default=100, ge=1, le=500)
    # Resume the previous page on the same namespace; unset for the first page.
    cursor: str | None = None
    # The output fields the caller reads (a whitelist
    # resolved against the row model; an unknown name is a 422); unset, every
    # field. Every list operation is projectable.
    fields: list[str] | None = None


class _GrantEntry(ProjectedRow):
    subject: str
    role: GrantRoleStr
    status: GrantStatusStr = "active"
    # When the user has requested elevation (writer/admin) on a gated
    # namespace, `pending_role` carries what they're queued for. Their
    # base `role` (typically viewer) is what they can do TODAY. UI
    # renders both: "alice (viewer · requesting writer)".
    pending_role: GrantRoleStr | None = None
    pending_since: datetime | None = None
    granted_at: datetime
    granted_by: str
    # The person's other spellings whose rows folded onto
    # this one (a login handle's verified email, or the reverse).
    also: list[str] = Field(default_factory=list)


class GrantsListOutput(BaseModel):
    # Live grants on the namespace, active and pending (revoked ones excluded), newest
    # first; one row per person, their other spellings in `also`.
    rows: list[_GrantEntry]
    # Pass as `cursor` for the next page; None when this page is the last.
    next_cursor: str | None = None
    # True when more people follow this page (`next_cursor` is set).
    truncated: bool = False


class GrantsPendingForMeInput(BaseModel):
    org: str
    # At most this many requests, newest first; 1 to 2,000.
    limit: int = Field(default=500, ge=1, le=2000)


class _PendingForMeEntry(BaseModel):
    namespace: str
    kind: str = "memory"
    subject: str
    current_role: GrantRoleStr
    requested_role: GrantRoleStr
    pending_since: datetime | None = None


class GrantsPendingForMeOutput(BaseModel):
    """The approvals a caller can act on, org-wide, in one
    call — every pending request in the namespaces they administer."""

    # Pending join or elevation requests in namespaces the caller administers, newest first;
    # empty for an anonymous caller.
    rows: list[_PendingForMeEntry]


class GrantsRevokeInput(BaseModel):
    org: str
    namespace: str
    # Whose grant to revoke: a login handle, an email or a service-account `sub`; the
    # person's row under each verified spelling goes, and a pending request is rejected.
    # An address no verified account holds that never held a row here takes the rows of
    # the unverified accounts naming it.
    subject: str


class GrantsRevokeOutput(BaseModel):
    # True when any of the subject's grant rows was found and revoked; false when it had none.
    revoked: bool
    # When the revoke was written (UTC); None when nothing was revoked.
    revoked_at: datetime | None = None


class GrantsApproveInput(BaseModel):
    """Approve (or reject) a pending grant request.

    A pending grant arrives when a public namespace has
    `requires_approval=true` and the joiner asked for writer. Admin
    promotes it to active with `decision="approve"`, or removes it
    entirely (same effect as grants.revoke) with `decision="reject"`.
    """

    org: str
    namespace: str
    # Whose pending request to decide: an email or a service-account `sub` claim.
    subject: str
    # `approve` makes the requested (or overriding) role active; `reject` drops the request,
    # keeping any base role, or removes the row when the subject had no base access.
    decision: Literal["approve", "reject"] = "approve"
    # Approve with a different role than was asked for
    # (a maintainer request answered as reader). None = as asked.
    role: GrantRoleStr | None = None


class GrantsApproveOutput(BaseModel):
    # The subject as the request spelled it.
    subject: str
    namespace: str
    # The decision applied (echoes the request).
    decision: Literal["approve", "reject"]
    # The role approved (for `reject`, the role the request would have had); None when
    # no grant row existed for the subject.
    role: GrantRoleStr | None = None
    # When the decision was written (UTC).
    decided_at: datetime


class GrantsShareAgentInput(BaseModel):
    org: str
    # The calling agent's own namespace, the only one it may share.
    namespace: str
    # The agent of the same trust to share it with, as its control plane names it.
    # Not `agent`: clients fill that identity field from the caller.
    grantee: str
    # What that agent may do there: `viewer` reads, `writer` also writes.
    role: Literal["viewer", "writer"] = "viewer"


class GrantsShareAgentOutput(BaseModel):
    namespace: str
    # The subject the grant is stored under, `sa:<trust>:<agent>`.
    subject: str
    # The role now active for that agent.
    role: Literal["viewer", "writer"]
    # When the grant row was written (UTC).
    granted_at: datetime


class GrantsUnshareAgentInput(BaseModel):
    org: str
    # The calling agent's own namespace.
    namespace: str
    # The agent of the same trust whose grant there to revoke.
    grantee: str


class GrantsUnshareAgentOutput(BaseModel):
    # True when the agent held a grant and it was revoked.
    revoked: bool
    # When the revoke was written (UTC); None when nothing was revoked.
    revoked_at: datetime | None = None
