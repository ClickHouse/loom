"""What a domain that registers namespaces needs from identity.

A code repository or a tracker source reads the namespace directory and makes
its creator the admin through these, not through identity's store.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from server.core.repo_base import _CHClient
from server.identity.store import grants as grants_repo
from server.identity.store import namespaces as namespaces_repo


async def namespace_row(
    org: str, namespace: str, *, ch: _CHClient | None = None
) -> dict[str, Any] | None:
    """The directory row of (org, namespace), every column (a re-insert must carry each
    one forward), active or soft-deleted; None when the name was never registered."""
    return await namespaces_repo.fetch_row(org, namespace, ch=ch)


async def namespace_is_active(org: str, namespace: str, *, ch: _CHClient | None = None) -> bool:
    """Is (org, namespace) a registered, active namespace? A soft-deleted one is not."""
    return await namespaces_repo.is_active(org, namespace, ch=ch)


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


__all__ = ["grant_self_admin", "namespace_is_active", "namespace_row"]
