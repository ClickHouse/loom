"""Revoking a subject: what `loom revoke-token` and `POST /v1/admin/revoke_subject`
share, so the two cannot drift.

The subject's row in loom.revoked_tokens (`revocation.revoke`) refuses every
token whose `sub` is the subject, every token it made or that descends from one
of its tokens (their `creator` and `parent_subjects`), and every refresh of its
sign-in sessions; every mint and refresh reads revocations from ClickHouse, so
none makes it a new token either. Outright, the row refuses all of them; with a
cutoff, those issued before it, so a later sign-in works. The tokens it ends
are then marked revoked in their token lists, freeing their names, as a SCIM
deactivation does (`service_accounts.revoke_created_by`).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.auth import revocation
from server.core.datetimes import as_aware_utc
from server.core.errors import GrantBusyError
from server.core.principals import is_reserved_subject, org_of_end_user_revocation
from server.observability import audit

NO_TOKEN = (
    "no token is revoked by that subject; revoke an end user as "
    "<org>/user:<id>, the id as its issuer signs it (lowercase)"
)
# Leases are named from a hash of the subject: the lease store is no place for an address.
_LEASE_NAMESPACE = uuid.UUID("0e3a7c52-41f9-4d6b-8a2e-5c9b17d4f603")


def refusal(subject: str) -> str | None:
    """Why revoking `subject` would end no token, or None."""
    if not subject.strip():
        return "a subject is required"
    if "\x00" in subject:  # the prefixed keys are an operator cutoff's and SCIM's
        return "a subject may not contain a NUL character"
    if is_reserved_subject(subject) and not org_of_end_user_revocation(subject):
        # No token's sub is a bare principal, and an end user's key is built from
        # a lowercase id under a named org: any other spelling cuts off no one.
        return NO_TOKEN
    return None


def cutoff(issued_before: datetime) -> datetime:
    """The cutoff that ends every token issued before `issued_before` on any
    replica: its second, and a clock running ahead of the one that read it."""
    return revocation.past_every_token_before(as_aware_utc(issued_before))


async def audit_revocation(
    row: dict[str, Any],
    *,
    token_id: str,
    kind: str,
    by: str,
    org: str,
    scim: bool = False,
    **extra: Any,
) -> None:
    """The durable record of a revocation: identifiers only, never token
    material. A subject's revocation spans every org; `org` is where it is
    recorded ('-' for the deployment, an end user's own org for theirs)."""
    await audit.emit(
        "token.revoked",
        org=org,
        namespace="-",
        actor=by,
        subject={
            "matched_claim": kind,
            "token_id": token_id,
            "scim": scim,
            "issued_before": row["issued_before"],
            "expires_at": row["expires_at"],
            "reason": row["reason"],
            **extra,
        },
    )


@dataclass(frozen=True, slots=True)
class Revoked:
    """A subject revoked for good: the row in force (written now, or `kept`, an
    earlier one that already covered the request), how many of its sign-in
    sessions it ends, and how many tokens it made (directly or through its
    tokens) it ends, each now listed revoked."""

    row: dict[str, Any]
    sessions: int
    tokens: int
    kept: bool


@asynccontextmanager
async def _held(subject: str) -> AsyncIterator[None]:
    """The subject's own row read and written as one step: a cutoff never lands
    between another call's read and its outright revocation."""
    from server.core import leases

    name = f"subject-revocation:{uuid.uuid5(_LEASE_NAMESPACE, subject)}"
    async with leases.holding(name, ttl_s=60.0, wait_s=10.0) as held:
        if not held:
            raise GrantBusyError("another revocation of this subject is running; retry shortly")
        yield


def _covers(row: dict[str, Any], at: datetime | None) -> bool:
    """Whether the subject's own row already refuses, for good, everything a
    revocation outright (`at` None) or cut off at `at` would: it never lapses,
    and it is outright or cuts off at `at` or later."""
    if not revocation.never_lapses(row):  # a time-boxed one gives way to one for good
        return False
    held = row["issued_before"]
    return held is None or (at is not None and as_aware_utc(held) >= at)


async def _write(
    subject: str, at: datetime | None, *, reason: str, by: str
) -> tuple[dict[str, Any], bool]:
    """Revoke `subject` outright (`at` None) or for tokens issued before `at`,
    unless its own row already covers that: an outright revocation, or a later
    cutoff, stays as it is. The row in force, and whether it was kept."""
    async with _held(subject):
        current = await revocation.own_row(subject)
        if current is not None and _covers(current, at):
            return current, True
        return await revocation.revoke(
            subject, reason=reason, revoked_by=by, issued_before=at
        ), False


async def revoke_subject(
    subject: str,
    *,
    by: str,
    org: str,
    reason: str = "",
    issued_before: datetime | None = None,
) -> Revoked:
    """Revoke every token and sign-in session of `subject` for good, outright or
    (with `issued_before`) those issued before it, never weakening a revocation
    it finds, then mark the tokens it ends revoked. The caller has checked
    `refusal`. Run again, it finishes what a failed try left and answers the
    same counts. Raises when the revocation did not land, or (after it landed,
    and its audit row with it) when marking the tokens did not finish."""
    from server.auth import oauth_store, service_accounts

    subject = subject.strip()  # as `revocation.revoke` keys it
    at = None if issued_before is None else cutoff(issued_before)
    row, kept = await _write(subject, at, reason=reason, by=by)
    held = row["issued_before"]
    before = None if held is None else int(as_aware_utc(held).timestamp())
    sessions, tokens = 0, 0
    try:
        # A session a logout or a refusal ended has its own revocation; one
        # refreshed since the cutoff lives on.
        families = await oauth_store.session_families(subject)
        sessions = sum(
            revocation.kills(before, newest) and not revocation.is_revoked(family)
            for family, newest in families.items()
        )
        tokens = len(await service_accounts.revoke_created_by([subject], revoked_by=by))
    finally:  # a revocation that landed is recorded, even if the marking did not finish
        end_user_org = org_of_end_user_revocation(subject)
        await audit_revocation(
            row,
            token_id=subject,
            kind="subject",
            by=by,
            org=end_user_org or org,
            sessions=sessions,
            tokens=tokens,
            kept=kept,
        )
    return Revoked(row=row, sessions=sessions, tokens=tokens, kept=kept)


async def revoke_in_org(
    org: str,
    subject: str,
    *,
    by: str,
    reason: str = "",
    issued_before: datetime | None = None,
) -> list[tuple[str, Revoked]]:
    """`revoke_subject` for `subject` and every spelling `org`'s directory ties
    to it, as grants read them (`aliases`): an email reaches the login handle
    of the account it signs in to, a handle its email. Raises, revoking
    nothing, when the directory cannot be read."""
    from server.auth.aliases import spellings

    return [
        (s, await revoke_subject(s, by=by, org=org, reason=reason, issued_before=issued_before))
        for s in await spellings(org, subject.strip())
    ]
