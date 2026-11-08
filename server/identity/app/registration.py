"""What other domains need from identity's namespace directory: registering a
namespace, and reading its row and retention.

A code repository or a tracker source reads the namespace directory and makes its
creator the admin through these, and a report reads a namespace's retention here, not
through identity's store.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.core.errors import AuthInsufficientError
from server.core.repo_base import _CHClient
from server.identity.store import grants as grants_repo
from server.identity.store import namespaces as namespaces_repo


async def namespace_row(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The directory row of (org, namespace), every column (a re-insert must carry each
    one forward), active or soft-deleted; None when the name was never registered."""
    return await namespaces_repo.fetch_row(org, namespace, ch=ch)


async def classifications(
    org: str, namespaces: list[str], *, ch: _CHClient | None = None
) -> dict[str, str]:
    """The classification of each of `namespaces` that has a directory row, in one read."""
    return await namespaces_repo.classifications(org, namespaces, ch=ch)


async def namespace_retention_days(org: str, namespace: str, *, ch: _CHClient | None = None) -> int:
    """The namespace's own `retention_days`, which sets its ledger's TTL over the table's
    global one (0 when it sets none or is not registered)."""
    row = await namespace_row(org, namespace, ch=ch)
    return int(row.get("retention_days") or 0) if row else 0


async def refused_as_in_use(org: str, namespace: str, *, ch: _CHClient | None = None) -> bool:
    """Whether (org, namespace) is in use (`directory().in_use`) and the caller fails the
    gate's ADMIN check on it there. A path that registers a name as new refuses it then.
    Auth off refuses nobody."""
    from server.auth.authz import Role, row_reach
    from server.auth.directory import directory
    from server.auth.fail_closed import note_answered_unverified

    verdict = await row_reach(org, namespace, Role.ADMIN)
    if verdict or not await directory().in_use(org, namespace, ch=ch):
        return False
    if verdict is None:
        note_answered_unverified()
    return True


def not_admin_error(org: str, namespace: str, caller: str) -> AuthInsufficientError:
    """The refusal of a non-admin who would rewrite a registered name or register an
    in-use one as new: one answer, so the two cannot be told apart."""
    return AuthInsufficientError(
        f"{caller!r} is not an admin of {org}/{namespace}; cannot update an existing namespace"
    )


async def namespace_claimable(org: str, namespace: str) -> bool:
    """Whether a domain may register (org, namespace) as new for the caller: no
    directory row and not refused as in use (`refused_as_in_use`), or a soft-deleted
    row the caller administers. A soft-deleted namespace keeps its data and grant rows,
    so registering it again would make anyone else, a subject whose grant there was
    revoked included, its admin."""
    from server.auth.authz import Role, grant_role_for
    from server.auth.context import current_caller
    from server.auth.default_namespace import auth_off_subject

    row = await namespace_row(org, namespace)
    if row is None:
        return not await refused_as_in_use(org, namespace)
    subject = current_caller()[0] or auth_off_subject()
    if row.get("active") is not False or not subject:
        return False
    role = await grant_role_for(org, namespace, subject)
    return role is not None and role.value >= Role.ADMIN.value


async def grant_self_admin(org: str, namespace: str, subject: str) -> None:
    """An active admin grant for `subject` on (org, namespace), granted by the subject:
    the creator of a namespace administers it."""
    now = datetime.now(UTC).replace(tzinfo=None)
    await grants_repo.insert_row(
        {
            "org": org,
            "namespace": namespace,
            "subject": subject,
            "role": "admin",
            "status": "active",
            "pending_role": None,
            "pending_since": None,
            "granted_at": now,
            "granted_by": subject,
            "revoked_at": None,
            "version": time.time_ns(),
        }
    )


__all__ = [
    "grant_self_admin",
    "namespace_claimable",
    "namespace_retention_days",
    "namespace_row",
    "not_admin_error",
    "refused_as_in_use",
]
