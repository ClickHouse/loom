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
from datetime import UTC, datetime
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SACreator:
    """The unrevoked service account's `created_by`, and whether the account
    reads through that creator's grants."""

    subject: str
    inherits: bool


# (org, sa_subject) -> (time.monotonic() of the read, its creator). Kept for
# the revocation refresh interval, as `delegation._creator_account` keeps the
# creator's account; a revoke also revokes the token by subject at once. The
# cap bounds it.
_creators: dict[tuple[str, str], tuple[float, SACreator]] = {}
_CREATOR_CACHE_MAX = 4096


async def sa_creator(org: str, sa_subject: str, *, ch: Any | None = None) -> SACreator | None:
    """The creator of the unrevoked service account `sa_subject` in `org`;
    None for a subject that is not one, a row with no creator, or a failed
    read (fail closed: no inheritance). A found row is cached for the
    revocation refresh interval; a miss or a failed read is not."""
    if not sa_subject.startswith("sa:"):
        return None
    key = (org, sa_subject)
    hit = _creators.get(key)
    if hit is not None and time.monotonic() - hit[0] < settings.jwt_revocation_cache_ttl_seconds:
        return hit[1]
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
    found = SACreator(subject=creator, inherits=bool((row or {}).get("inherit_creator_grants")))
    if len(_creators) >= _CREATOR_CACHE_MAX:
        _creators.clear()
    _creators[key] = (time.monotonic(), found)
    return found


async def inheriting_creator(org: str, sa_subject: str, *, ch: Any | None = None) -> str | None:
    """The creator whose live grants `sa_subject` reads through, or None: the
    account does not inherit, has no creator, or is its own creator."""
    sa = await sa_creator(org, sa_subject, ch=ch)
    if sa is None or not sa.inherits or sa.subject == sa_subject:
        return None
    return sa.subject


def revoked_row(row: dict[str, Any], *, revoked_at: datetime, revoked_by: str) -> dict[str, Any]:
    """A new version of the `loom.service_accounts` row `row`, revoked. Every
    column is carried forward: one left out reads as its default under FINAL.
    Its version outranks the row's, whichever host's clock wrote that one."""
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
        "version": max(time.time_ns(), int(row["version"]) + 1),
        "kind": row["kind"],
        "client": row["client"],
        "inherit_creator_grants": row["inherit_creator_grants"],
        "ns_roles": row["ns_roles"],
    }


# Names a pass revokes at once: each is a lease round trip and an insert.
_CLAIMS_AT_ONCE = 8
# Creators read at once: their keys are one array parameter, which ClickHouse
# refuses past its HTTP field size (a few thousand subjects).
_CREATORS_AT_ONCE = 500
_UNEXPIRED = "(expires_at IS NULL OR expires_at > now())"


async def revoke_created_by(subjects: list[str], *, revoked_by: str) -> None:
    """Mark revoked each token any of `subjects` made, directly or through
    their tokens, whose token the revocations kill, so its name comes free;
    one made after a cutoff stays live. A retry finds only what an earlier
    try left."""
    from server.auth import revocation
    from server.auth.delegation import MAX_CHAIN_DEPTH
    from server.core.ch import get_client, own_write_reads

    ch = get_client()
    seen = set(subjects)
    creators = sorted(seen)
    dead: set[str] = set()  # the `sa:` subjects of the dead tokens found so far
    # One pass per generation: the person's tokens, then the tokens those made.
    for _ in range(MAX_CHAIN_DEPTH + 1):
        if not creators:
            return
        made: set[str] = set()
        for i in range(0, len(creators), _CREATORS_AT_ONCE):
            chunk = creators[i : i + _CREATORS_AT_ONCE]
            with own_write_reads():
                rows = await ch.query_all(
                    "SELECT org, name, sa_subject, created_by, created_at, "
                    "revoked_at IS NULL AS live FROM loom.service_accounts FINAL "
                    f"WHERE created_by IN {{subs:Array(String)}} AND {_UNEXPIRED}",
                    params={"subs": chunk},
                )
            # Read after the list, so it tells whether each token listed is dead.
            revoked = await revocation.cutoffs(chunk)
            # Only those still dead: an operator may have let the creator live since.
            dead |= {
                str(r["sa_subject"]) for r in rows if not r["live"] and _is_dead(r, revoked, dead)
            }
            live = [r for r in rows if r["live"]]
            dead |= await _revoke_all(live, chunk, revoked, dead, revoked_by)
            # Revoked ones too: a retry still reaches what their tokens made.
            made |= {str(r["sa_subject"]) for r in rows}
        made -= seen
        seen |= made
        creators = sorted(made)


def _is_dead(row: dict[str, Any], revoked: dict[str, int | None], dead: set[str]) -> bool:
    """Whether the token `row` lists is dead: the token that made it is, or its
    creator is revoked outright, or narrowed to a cutoff later than the row's
    `created_at`. Judged by the row's `created_at`, which is never earlier
    than the chain's oldest `iat`: a token this calls live may still be dead
    at the gate (a stale replica let an old session make it), never the
    reverse."""
    creator = str(row["created_by"])
    if creator in dead:
        return True
    if creator not in revoked:
        return False
    cutoff = revoked[creator]
    made = row["created_at"]
    if made.tzinfo is None:
        made = made.replace(tzinfo=UTC)
    return cutoff is None or int(made.timestamp()) < cutoff


async def _revoke_all(
    found: list[dict[str, Any]],
    creators: list[str],
    revoked: dict[str, int | None],
    dead: set[str],
    revoked_by: str,
) -> set[str]:
    """Revoke those of the rows `found` whose token is dead, each name claimed
    as a create claims it and read again under it: a revoked copy of a row
    read before must not replace a newer one with the name. A task holds one
    claim at a time, so two passes over the same names never wait on each
    other in a cycle, and each row that lands stays revoked for a retry.
    Returns the `sa:` subjects revoked."""
    from server.auth import revocation
    from server.auth.grant_claims import service_account_change
    from server.core.ch import get_client, own_write_reads
    from server.core.datetimes import naive_utc_now_seconds

    ch = get_client()
    gate = asyncio.Semaphore(_CLAIMS_AT_ONCE)
    listed = {str(r["sa_subject"]) for r in found}

    async def revoke(org: str, name: str) -> str | None:
        async with gate, service_account_change(org, name):
            with own_write_reads():
                row = await ch.query_one(
                    "SELECT * FROM loom.service_accounts FINAL "
                    "WHERE org = {org:String} AND name = {name:String} "
                    "AND created_by IN {subs:Array(String)} AND revoked_at IS NULL "
                    f"AND {_UNEXPIRED}",
                    params={"org": org, "name": name, "subs": creators},
                )
            if row is None:
                return None
            row = dict(row)
            # A token made since the list may postdate `revoked` (a reactivation's).
            now_revoked = revoked
            if row["sa_subject"] not in listed:
                now_revoked = await revocation.cutoffs([row["created_by"]])
            if not _is_dead(row, now_revoked, dead):
                return None
            revoked_copy = revoked_row(
                row, revoked_at=naive_utc_now_seconds(), revoked_by=revoked_by
            )
            await ch.insert("loom.service_accounts", [revoked_copy])
            return str(row["sa_subject"])

    try:
        async with asyncio.TaskGroup() as tasks:
            done = [tasks.create_task(revoke(r["org"], r["name"])) for r in found]
    except ExceptionGroup as e:  # the failure itself, which the caller maps to a 503
        raise e.exceptions[0] from e
    return {subject for task in done if (subject := task.result()) is not None}


__all__ = ["SACreator", "inheriting_creator", "revoke_created_by", "revoked_row", "sa_creator"]
