"""Typed rows the grants store hands the identity app.

A grant row is keyed (org, namespace, subject); `role` is what the subject
holds now and `status` whether it is live (`active`) or a request that is
not yet approved (`pending`). An active grant can also carry an elevation
request in `pending_role`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

# The roles a grant row holds (the wire's GrantRoleStr).
GrantRole = Literal["viewer", "writer", "admin"]
# A live row's status (the wire's GrantStatusStr).
GrantStatus = Literal["active", "pending"]


@dataclass(frozen=True, slots=True)
class GrantState:
    """One subject's live (not revoked) grant row on one namespace."""

    role: GrantRole
    granted_by: str
    # '' reads as active.
    status: str
    pending_role: GrantRole | None
    pending_since: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GrantState:
        return cls(
            role=cast(GrantRole, str(row.get("role") or "")),
            granted_by=str(row.get("granted_by") or ""),
            status=str(row.get("status") or ""),
            pending_role=row.get("pending_role") or None,
            pending_since=row.get("pending_since"),
        )


@dataclass(frozen=True, slots=True)
class GrantPerson:
    """One person's newest grant, highest active role, and outstanding request."""

    subject: str
    role: GrantRole
    status: GrantStatus
    pending_role: GrantRole | None
    pending_since: datetime | None
    granted_at: datetime
    granted_by: str
    also: tuple[str, ...]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> GrantPerson:
        return cls(
            subject=str(row["subject"]),
            role=cast(GrantRole, str(row["role"])),
            # '' reads as active.
            status=cast(GrantStatus, str(row.get("status") or "active")),
            pending_role=row.get("pending_role") or None,
            pending_since=row.get("pending_since"),
            granted_at=row["granted_at"],
            granted_by=str(row.get("granted_by") or ""),
            also=tuple(str(s) for s in row.get("also") or ()),
        )


@dataclass(frozen=True, slots=True)
class PendingGrant:
    """A pending join or elevation request in a namespace an admin decides."""

    namespace: str
    kind: str
    subject: str
    role: GrantRole
    status: str
    pending_role: GrantRole | None
    pending_since: datetime | None
    granted_at: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PendingGrant:
        return cls(
            namespace=str(row["namespace"]),
            kind=str(row.get("kind") or "memory"),
            subject=str(row["subject"]),
            role=cast(GrantRole, str(row["role"])),
            status=str(row.get("status") or ""),
            pending_role=row.get("pending_role") or None,
            pending_since=row.get("pending_since"),
            granted_at=row.get("granted_at"),
        )


__all__ = ["GrantPerson", "GrantRole", "GrantState", "GrantStatus", "PendingGrant"]
