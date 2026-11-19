"""Revoking a subject: what `loom revoke-token` and `POST /v1/admin/revoke_subject`
share, so the two cannot drift.

Outright, the subject's own row in loom.revoked_tokens (`revocation.revoke`)
refuses every token whose `sub` is the subject, every token it made or that
descends from one of its tokens (their `creator` and `parent_subjects`), and
every refresh of its sign-in sessions. Up to an instant, a cutoff does the same
for what was issued before it, so a later sign-in works; it is written beside
the subject's own row (`revocation.cut_off`), so whatever that row says stays
in force. Every mint and refresh reads revocations from ClickHouse, so neither
makes the subject a token it refuses. The tokens it ends are then marked
revoked in their token lists, freeing their names, as a SCIM deactivation does
(`service_accounts.revoke_created_by`).

`record` writes the revocation (or finds one that already covers it) and
`finish` counts and marks what it ends and audits it. A bounded caller treats a
lost write or claim-release acknowledgement as unsettled, even during `record`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from server.auth import revocation
from server.core.datetimes import as_aware_utc
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


def cutoff(issued_before: datetime) -> datetime:
    """The cutoff that ends every token issued before `issued_before` on any
    replica: its second, and a clock running ahead of the one that read it. An
    instant ahead of this one counts as now: a cutoff later than the replicas'
    clocks would refuse sign-ins still to come."""
    at = revocation.past_every_token_before(as_aware_utc(issued_before))
    return min(at, revocation.from_now_on())


async def audit_revocation(
    row: dict[str, Any],
    *,
    token_id: str,
    kind: str,
    by: str,
    org: str,
    scim: bool = False,
    deadline: float | None = None,
    **extra: Any,
) -> None:
    """The durable record of a revocation: identifiers only, never token
    material. A subject's revocation spans every org; `org` is where it is
    recorded ('-' for the deployment, an end user's own org for theirs)."""
    if deadline is not None:
        remaining = min(2.0, deadline - time.monotonic())
        if remaining <= 0:
            return  # This ordinary best-effort audit is not the feature's completion proof.
        from server.core.ch import statement_timeout

        try:
            async with asyncio.timeout(remaining):
                with statement_timeout(remaining):
                    await audit_revocation(
                        row,
                        token_id=token_id,
                        kind=kind,
                        by=by,
                        org=org,
                        scim=scim,
                        **extra,
                    )
        except TimeoutError:
            return
        return
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
    """A subject revoked for good: the row in force for the request (written
    now, or `kept`, an earlier one that already covered it), whether the subject
    is now revoked `outright` (by that row, or one beside it) or else the
    `cutoff` in force across its revocations, how many of its sign-in sessions
    it ends, and how many tokens it made (directly or through its tokens) it
    ends, each now listed revoked."""

    row: dict[str, Any]
    kept: bool
    outright: bool
    cutoff: datetime | None
    sessions: int
    tokens: int


async def record(
    subject: str,
    *,
    by: str,
    reason: str = "",
    at: datetime | None = None,
    deadline: float | None = None,
    ch: Any = None,
    progress: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Revoke `subject` for good: outright (`at` None), on its own key, unless
    that key already revokes it outright for good; or up to the cutoff `at` (from
    `cutoff`), on the cutoff key beside it, unless that key already holds a
    later cutoff. The subject's own row is never touched by a cutoff, so an
    outright revocation stays, a time-boxed one until it lapses. Returns the row
    in force for the request and whether it was there already. A lost write or
    release acknowledgement may leave the row in force."""
    subject = subject.strip()  # as `revocation.revoke` keys it
    if deadline is not None and at is not None:
        raise ValueError("A bounded subject action is outright only")
    if at is not None:
        return await revocation.record_cutoff(
            subject, issued_before=at, reason=reason, revoked_by=by
        )
    bounded: dict[str, Any] = {} if deadline is None else {"deadline": deadline, "ch": ch}
    async with revocation.subject_change(subject, **bounded):
        current = (
            await revocation.stored(subject)
            if deadline is None
            else await revocation.stored_bounded(subject, **bounded)
        )
        if (
            current is not None
            and current["issued_before"] is None
            and revocation.never_lapses(current)
        ):
            if progress is not None:
                progress["recorded"] = True
            revocation.remember_row(current, **({} if deadline is None else {"bounded": True}))
            return current, True
        if deadline is None:
            row = await revocation.revoke(subject, reason=reason, revoked_by=by)
        else:
            row = await revocation.revoke(
                subject,
                reason=reason,
                revoked_by=by,
                deadline=deadline,
                ch=ch,
                progress=progress,
            )
        if progress is not None:
            progress["recorded"] = True
        return row, False


async def finish(
    subject: str,
    row: dict[str, Any],
    kept: bool,
    *,
    by: str,
    org: str,
    deadline: float | None = None,
    ch: Any = None,
    progress: dict[str, int] | None = None,
) -> Revoked:
    """After `record`: count the sign-in sessions the subject's revocations end
    (a logout or a refusal ended some already; one refreshed since a cutoff
    lives on), mark revoked the tokens they end, and audit it. Raises, the
    revocation in force all the same, when marking the tokens did not finish;
    run again, it finishes and answers the same counts."""
    from server.auth import oauth_store, service_accounts
    from server.auth.account_lifecycle import stop_connections

    subject = subject.strip()
    outright, in_force, sessions, tokens = False, None, 0, 0
    try:
        revoked = (
            await revocation.cutoffs([subject])
            if deadline is None
            else await revocation.cutoffs_bounded([subject], deadline=deadline, ch=ch)
        )
        await stop_connections([subject], respect_cutoff=True)
        if subject in revoked:
            at = revoked[subject]
            outright = at is None
            in_force = None if at is None else datetime.fromtimestamp(at, UTC)
            families = (
                await oauth_store.session_families(subject)
                if deadline is None
                else await oauth_store.session_families(subject, deadline=deadline, ch=ch)
            )
            sessions = sum(
                revocation.kills(at, newest) and not revocation.is_revoked(family)
                for family, newest in families.items()
            )
            if progress is not None:
                progress["sessions"] = sessions
        if deadline is None:
            tokens = len(await service_accounts.revoke_created_by([subject], revoked_by=by))
        else:
            tokens = len(
                await service_accounts.revoke_created_by_bounded(
                    subject,
                    revoked_by=by,
                    deadline=deadline,
                    ch=ch,
                    progress=progress if progress is not None else {},
                )
            )
    finally:  # a revocation that landed is recorded, even if the marking did not finish
        end_user_org = org_of_end_user_revocation(subject)
        if deadline is None:
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
        else:
            await audit_revocation(
                row,
                token_id=subject,
                kind="subject",
                by=by,
                org=end_user_org or org,
                sessions=sessions,
                tokens=tokens,
                kept=kept,
                deadline=deadline,
            )
    return Revoked(
        row=row, kept=kept, outright=outright, cutoff=in_force, sessions=sessions, tokens=tokens
    )


async def revoke_subject(
    subject: str, *, by: str, org: str, reason: str = "", at: datetime | None = None
) -> Revoked:
    """`record` then `finish`. The caller has checked `refusal`."""
    row, kept = await record(subject, by=by, reason=reason, at=at)
    return await finish(subject, row, kept, by=by, org=org)


async def revoke_in_org(
    org: str, subject: str, *, by: str, reason: str = "", at: datetime | None = None
) -> list[tuple[str, Revoked]]:
    """`revoke_subject` for `subject` and every spelling `org`'s directory
    pairs with it, its email verified or not (`aliases.spellings`): an email
    (and its lower case) reaches the login handle of the account naming it, a
    handle its email. Raises, revoking nothing, when the directory cannot be read."""
    from server.auth.aliases import spellings

    recorded = []
    errors = []
    for s in await spellings(org, subject.strip()):
        try:
            recorded.append((s, await record(s, by=by, reason=reason, at=at)))
        except Exception as e:  # noqa: BLE001 — finish every revocation that was recorded
            errors.append(e)
    completed = []
    for s, (row, kept) in recorded:
        try:
            completed.append((s, await finish(s, row, kept, by=by, org=org)))
        except Exception as e:  # noqa: BLE001 — audit every recorded spelling before raising
            errors.append(e)
    if errors:
        raise errors[0]
    return completed
