"""Revoking a subject: what `loom revoke-token` and `POST /v1/admin/revoke_subject`
share, so the two cannot drift.

The subject's row in loom.revoked_tokens (`revocation.revoke`) refuses every
token whose `sub` is the subject, every token it made or that descends from one
of its tokens (their `creator` and `parent_subjects`), and every refresh of its
sign-in sessions; every mint and refresh reads revocations from ClickHouse, so
none makes it a new token either. A revocation for good then marks the tokens
the subject made revoked in their token lists, freeing their names, as a SCIM
deactivation does (`service_accounts.revoke_created_by`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.auth import revocation
from server.core.principals import is_reserved_subject, org_of_end_user_revocation
from server.observability import audit

NO_TOKEN = (
    "no token is revoked by that subject; revoke an end user as "
    "<org>/user:<id>, the id as its issuer signs it (lowercase)"
)


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


async def audit_revocation(
    row: dict[str, Any],
    *,
    token_id: str,
    kind: str,
    by: str,
    org: str,
    scim: bool = False,
    **counts: int,
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
            **counts,
        },
    )


@dataclass(frozen=True, slots=True)
class Revoked:
    """A subject revoked for good: its row as written, how many of its sign-in
    sessions it ends, and how many tokens it made (directly or through its
    tokens) it ends, each now listed revoked."""

    row: dict[str, Any]
    sessions: int
    tokens: int


async def revoke_subject(subject: str, *, by: str, org: str, reason: str = "") -> Revoked:
    """Revoke every token and sign-in session of `subject`, outright and for
    good, then mark the tokens it made revoked. The caller has checked
    `refusal`. Run again, it finishes what a failed try left and answers the
    same counts. Raises when the revocation did not land, or (after it landed,
    and its audit row with it) when marking the tokens did not finish."""
    from server.auth import oauth_store, service_accounts

    subject = subject.strip()  # as `revocation.revoke` keys it
    row = await revocation.revoke(subject, reason=reason, revoked_by=by)
    sessions, tokens = 0, 0
    try:
        # A session a logout or a refusal ended has its own revocation.
        families = await oauth_store.session_families(subject)
        sessions = sum(not revocation.is_revoked(f) for f in families)
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
        )
    return Revoked(row=row, sessions=sessions, tokens=tokens)


async def revoke_in_org(
    org: str, subject: str, *, by: str, reason: str = ""
) -> list[tuple[str, Revoked]]:
    """`revoke_subject` for `subject` and every spelling `org`'s directory ties
    to it, as grants read them (`aliases`): an email reaches the login handle
    of the account it signs in to, a handle its email. Raises, revoking
    nothing, when the directory cannot be read."""
    from server.auth.aliases import spellings

    return [
        (s, await revoke_subject(s, by=by, org=org, reason=reason))
        for s in await spellings(org, subject.strip())
    ]
