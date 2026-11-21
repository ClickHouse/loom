"""Access to unapproved definitions, which may contain a team's queries."""

from __future__ import annotations

from server.memory.domain.definitions import DEFINITION_DRAFT_KINDS
from server.settings import settings


async def private_candidates_visible(org: str, namespace: str) -> bool:
    """Only a namespace admin reads its unapproved gap and query-history drafts."""
    from server.auth.authz import CachedGate
    from server.auth.context import current_auth
    from server.operations.base import Role

    auth = current_auth()
    if auth is None:
        return not settings.auth_actually_required()
    return await CachedGate(auth, org).admits(namespace, Role.ADMIN)


async def candidate_visible(org: str, namespace: str, source_kind: str) -> bool:
    """Ordinary learnings remain reader-visible; unapproved definitions need admin."""
    return source_kind not in DEFINITION_DRAFT_KINDS or await private_candidates_visible(
        org, namespace
    )
