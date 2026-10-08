"""admin.users — org-wide membership view for platform admins.

Surfaces every subject that has held a namespace grant or owned a
namespace, with their (namespace, role) pairs and last-active time.
Admin-only: this enumerates the entire user base.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AdminUsersInput(BaseModel):
    org: str
    namespace: str | None = None
    # Also list subjects with neither a contact email nor a password (service accounts);
    # off, they are left out.
    include_service_accounts: bool = False
    # At most this many users, in subject order; 1 to 1,000.
    limit: int = Field(default=200, ge=1, le=1000)


class UserNamespaceGrant(BaseModel):
    namespace: str
    role: str
    is_owner: bool
    granted_at: datetime | None = None
    # Role queued via an unapproved elevation request; `role` is current.
    pending_role: str | None = None


class UserSummary(BaseModel):
    subject: str
    # Empty for service-account / programmatic callers.
    contact_email: str
    grants: list[UserNamespaceGrant]
    is_platform_admin: bool
    last_active: datetime | None = None
    # Distinguishes human signups from token-only / SSO-bridged identities.
    has_password_auth: bool = False


class AdminUsersOutput(BaseModel):
    # The page of users in subject order, each with its grants and owned namespaces.
    users: list[UserSummary]
    # Users matching the filters before `limit` was applied.
    total: int
    # Grants on this page with an unapproved elevation request.
    total_pending_elevations: int = 0
    # Users on this page with an audit row in the last 7 days.
    total_active_last_7d: int = 0
    # The configured platform-admin identities, sorted; empty unless the caller is a
    # platform admin (or auth is off).
    platform_admins_configured: list[str]
