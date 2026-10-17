"""A service account's creator, and whether the account reads through the
creator's grants.

A service-account token (`sa:` subject) carries no grants of its own. Its
`loom.service_accounts` row names who created it and whether it inherits
that creator's live grants (`inherit_creator_grants`: true for a token
minted from the dashboard's Settings, false for an agent-signup token). The gate
and every namespace listing read this one row through `sa_creator`, so
they agree on what the token may read. A revoked row is written through
`revoked_row`, here so the SCIM deactivation can write one too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
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


def revoked_row(row: dict[str, Any], *, revoked_at: datetime, revoked_by: str) -> dict[str, Any]:
    """A new version of the `loom.service_accounts` row `row`, revoked. Every
    column is carried forward: one left out reads as its default under FINAL."""
    return {
        "org": row["org"],
        "name": row["name"],
        "sa_subject": row["sa_subject"],
        "namespaces": row["namespaces"],
        "roles": row["roles"],
        "ttl_days": row["ttl_days"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": revoked_at,
        "revoked_by": revoked_by,
        "version": time.time_ns(),
        "kind": row["kind"],
        "client": row["client"],
        "inherit_creator_grants": row["inherit_creator_grants"],
        "ns_roles": row["ns_roles"],
    }


# Names a pass revokes at once: each is a lease round trip and an insert.
_CLAIMS_AT_ONCE = 8
_UNEXPIRED = "(expires_at IS NULL OR expires_at > now())"


async def revoke_created_by(subjects: list[str], *, revoked_by: str, before: datetime) -> None:
    """Mark revoked every live token any of `subjects` created up to `before`,
    directly or through their tokens, in any org. For subjects revoked
    outright, whose tokens are already dead: their names come free and the
    Tokens list says so. One made after `before` can only be one a
    reactivation let in since, and stays live. A retry finds only what an
    earlier try left."""
    from server.auth.delegation import MAX_CHAIN_DEPTH
    from server.core.ch import get_client, own_write_reads

    ch = get_client()
    seen = set(subjects)
    creators = sorted(seen)
    # One pass per generation: the person's tokens, then the tokens those made.
    for _ in range(MAX_CHAIN_DEPTH + 1):
        if not creators:
            return
        with own_write_reads():
            rows = await ch.query_all(
                "SELECT org, name, sa_subject, revoked_at IS NULL AS live "
                "FROM loom.service_accounts FINAL "
                "WHERE created_by IN {subs:Array(String)} AND created_at <= {before:DateTime} "
                f"AND {_UNEXPIRED}",
                params={"subs": creators, "before": before},
            )
        await _revoke_all([r for r in rows if r["live"]], creators, revoked_by, before)
        # Revoked ones too: a retry still reaches what their tokens made.
        made = {str(r["sa_subject"]) for r in rows} - seen
        seen |= made
        creators = sorted(made)


async def _revoke_all(
    found: list[dict[str, Any]], creators: list[str], revoked_by: str, before: datetime
) -> None:
    """Revoke the rows `found`, each name claimed as a create claims it and
    read again under it: a revoked copy of a row read before must not
    replace a newer one with the name. A task holds one claim at a time, so
    two passes over the same names never wait on each other in a cycle, and
    each row that lands stays revoked for a retry."""
    from server.auth.grant_claims import service_account_change
    from server.core.ch import get_client, own_write_reads
    from server.core.datetimes import naive_utc_now_seconds

    ch = get_client()
    gate = asyncio.Semaphore(_CLAIMS_AT_ONCE)

    async def revoke(org: str, name: str) -> None:
        async with gate, service_account_change(org, name):
            with own_write_reads():
                row = await ch.query_one(
                    "SELECT * FROM loom.service_accounts FINAL "
                    "WHERE org = {org:String} AND name = {name:String} "
                    "AND created_by IN {subs:Array(String)} AND revoked_at IS NULL "
                    f"AND created_at <= {{before:DateTime}} AND {_UNEXPIRED}",
                    params={"org": org, "name": name, "subs": creators, "before": before},
                )
            if row is not None:
                revoked = revoked_row(
                    dict(row), revoked_at=naive_utc_now_seconds(), revoked_by=revoked_by
                )
                await ch.insert("loom.service_accounts", [revoked])

    try:
        async with asyncio.TaskGroup() as tasks:
            for r in found:
                tasks.create_task(revoke(r["org"], r["name"]))
    except ExceptionGroup as e:  # the failure itself, which the caller maps to a 503
        raise e.exceptions[0] from e


__all__ = ["SACreator", "inheriting_creator", "revoke_created_by", "revoked_row", "sa_creator"]
