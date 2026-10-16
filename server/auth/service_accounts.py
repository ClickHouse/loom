"""A service account's creator, and whether the account reads through the
creator's grants.

A service-account token (`sa:` subject) carries no grants of its own. Its
`loom.service_accounts` row names who created it and whether it inherits
that creator's live grants (`inherit_creator_grants`: true for a token
minted from the dashboard's Settings, false for an agent-signup token). The gate
and every namespace listing read this one row through `sa_creator`, so
they agree on what the token may read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SACreator:
    """The unrevoked service account's `created_by`, and whether the account
    reads through that creator's grants."""

    subject: str
    inherits: bool


async def sa_creator(org: str, sa_subject: str, *, ch: Any | None = None) -> SACreator | None:
    """The creator of the unrevoked service account `sa_subject` in `org`;
    None for a subject that is not one, a row with no creator, or a failed
    read (fail closed: no inheritance)."""
    if not sa_subject.startswith("sa:"):
        return None
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    try:
        row = await ch.query_one(
            """
            SELECT created_by, inherit_creator_grants
            FROM loom.service_accounts FINAL
            WHERE org = {org:String} AND sa_subject = {sub:String}
              AND revoked_at IS NULL
            LIMIT 1
            """,
            params={"org": org, "sub": sa_subject},
        )
    except Exception as e:  # noqa: BLE001 — a failed read inherits nothing (fail closed)
        log.warning("service_accounts lookup failed for %s/%s: %s", org, sa_subject, e)
        return None
    creator = str((row or {}).get("created_by") or "")
    if not creator:
        return None
    return SACreator(subject=creator, inherits=bool((row or {}).get("inherit_creator_grants")))


async def inheriting_creator(org: str, sa_subject: str, *, ch: Any | None = None) -> str | None:
    """The creator whose live grants `sa_subject` reads through, or None: the
    account does not inherit, has no creator, or is its own creator."""
    sa = await sa_creator(org, sa_subject, ch=ch)
    if sa is None or not sa.inherits or sa.subject == sa_subject:
        return None
    return sa.subject


__all__ = ["SACreator", "inheriting_creator", "sa_creator"]
