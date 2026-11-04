"""grants.{create,list,revoke,approve} handlers."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from server.auth.grant_claims import admins_change, grant_change, group_change
from server.core import grant_cache
from server.core.cursors import decode_bound_cursor, encode_bound_cursor
from server.core.errors import GrantBusyError, ValidationError
from server.identity.api.models_grants import (
    GrantsApproveInput,
    GrantsApproveOutput,
    GrantsCreateInput,
    GrantsCreateOutput,
    GrantsListInput,
    GrantsListOutput,
    GrantsPendingForMeInput,
    GrantsPendingForMeOutput,
    GrantsRevokeInput,
    GrantsRevokeOutput,
    _GrantEntry,
    _PendingForMeEntry,
)
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo
from server.operations.base import grant_rank
from server.operations.read_projection import project_rows

log = logging.getLogger(__name__)


# A revoke that finds the grant made admin under it starts over this many times.
_REVOKE_TRIES = 3


def _version() -> int:
    return time.time_ns()


async def adopt_namespace(
    org: str, namespace: str, new_admin: str, *, actor: str
) -> datetime | None:
    """Install `new_admin` as admin on a namespace with no active admin grant
    (the recovery path for an orphaned namespace). Returns the grant time, or
    None, writing nothing, when the namespace still has an active admin. A
    collection is never adopted: only grants open one, and a platform admin
    already reaches it."""
    from server.auth.sharing import is_collection
    from server.core.ch import get_client

    if is_collection(namespace):
        raise ValidationError("a collection is not adopted; a platform admin can grant on it")
    ch = get_client()
    if await grants_repo.count_active_admins(org, namespace, ch=ch) > 0:
        return None
    now = datetime.now(UTC).replace(tzinfo=None)
    await grants_repo.insert_rows(
        [
            {
                "org": org,
                "namespace": namespace,
                "subject": new_admin,
                "role": "admin",
                "granted_at": now,
                "granted_by": actor,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        ],
        ch=ch,
    )
    return now


async def _enforce_code_ceiling(org: str, namespace: str, role: str) -> None:
    """On a code namespace `writer` does not exist; a reader (viewer) or maintainer (admin)
    may be granted or approved by anyone the transport's ADMIN gate let through: an org
    admin or a current maintainer of that repository."""
    from server.core.ch import get_client
    from server.identity.domain import join_policy as _jp

    if role == _jp.CODE_MEMBER_ROLE:
        return
    row = await get_client().query_one(
        """
        SELECT ifNull(kind, 'memory') AS kind FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String} LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    kind = str((row or {}).get("kind") or "memory")
    ceiling = _jp.code_role_ceiling_violation(kind, role)
    if ceiling:
        raise ValidationError(ceiling, code="ROLE_CEILING")


async def _refuse_accidental_downgrade(
    input: GrantsCreateInput,
    subject: str,
    may_replace: Callable[[GrantState], None] | None = None,
) -> GrantState | None:
    """The grants table replaces on (org, namespace, subject), so a `viewer` create for a
    current `admin` would lower them silently; this guard refuses that. Returns the
    subject's current active row (or None) so the write can carry
    its pending request forward; raises 409 when the requested role is below
    the current one and the caller did not say `replace=true`. `may_replace`
    runs on that row first, so its refusal is the one a caller sees."""
    existing = await grants_repo.find_active_grant(input.org, input.namespace, subject)
    if existing is None:
        return None
    if may_replace is not None:
        may_replace(existing)
    current = existing.role
    if grant_rank(current) > grant_rank(input.role) and not input.replace:
        from server.core.errors import GrantWouldDowngradeError

        raise GrantWouldDowngradeError(
            f"{input.subject!r} is currently {current} on {input.namespace!r}; "
            f"a {input.role} grant would lower them. Repeat grants.create with "
            f"replace=true to lower the role on purpose, or grants.revoke to "
            f"remove them.",
            current_role=current,
            requested_role=input.role,
        )
    return existing


def _refuse_approval_below(current: str, target: str, input: GrantsApproveInput) -> None:
    """Approving an elevation never lowers the base grant: lowering is
    `grants.create replace=true`, done on purpose, never on the side."""
    if grant_rank(target) >= grant_rank(current):
        return
    from server.core.errors import GrantWouldDowngradeError

    raise GrantWouldDowngradeError(
        f"{input.subject!r} is currently {current} on {input.namespace!r}; approving their "
        f"request as {target} would lower them. Approve at {current} or above, or lower "
        f"them with grants.create replace=true.",
        current_role=current,
        requested_role=target,
    )


def _enforce_principal_target(namespace: str, subject: str) -> None:
    """A personal collection keeps its one grant; a group, an agent or
    `everyone` holds grants only on collections; a `user:`/`group:`/`agent:`
    subject needs a well-formed id."""
    from server.auth import sharing
    from server.core.principals import AGENT, EVERYONE, GROUP, is_principal, is_reserved_subject

    if sharing.is_personal(namespace):
        raise ValidationError("a personal collection is shared by copying an item out of it")
    if is_reserved_subject(subject) and not is_principal(subject):
        raise ValidationError(f"{subject!r} is not a well-formed principal (prefix:id, lowercase)")
    collection_only = subject.startswith((GROUP, AGENT)) or subject == EVERYONE
    if collection_only and not sharing.is_collection(namespace):
        raise ValidationError("groups, agents and everyone hold grants on collections only")


@asynccontextmanager
async def _group_held(org: str, subject: str) -> AsyncIterator[None]:
    """For a grant to a group, the group's claim from the check that it exists
    to the grant's write, so a SCIM delete cannot pass between them and leave
    the grant to a group made later under its id. Nothing for anyone else."""
    from server.core.principals import GROUP, principal_id
    from server.identity.app.scim_groups import fetch_group

    group_id = principal_id(subject, GROUP)
    if not group_id:
        yield
        return
    async with group_change(org, group_id):
        if await fetch_group(org, group_id) is None:
            raise ValidationError(f"no group {group_id!r} in {org}")
        yield


async def handle_grants_create(input: GrantsCreateInput) -> GrantsCreateOutput:
    """Insert or raise a grant; lower one only on `replace=true`. Admin-issued grants are
    always active (the admin is the approver),
    so no approval step. A pending elevation the new role does not satisfy
    is carried forward; one it satisfies is answered by the grant."""
    return await create_grant(input)


async def create_grant(
    input: GrantsCreateInput, may_replace: Callable[[GrantState], None] | None = None
) -> GrantsCreateOutput:
    """`grants.create`; `may_replace` raises for a live row this caller must
    not write over, checked on the row read under the grant's claim."""
    await _enforce_code_ceiling(input.org, input.namespace, input.role)
    _enforce_principal_target(input.namespace, input.subject)
    async with _group_held(input.org, input.subject):
        now = await _create_grant(input, may_replace)
    return GrantsCreateOutput(
        org=input.org,
        namespace=input.namespace,
        subject=input.subject,
        role=input.role,
        granted_at=now,
    )


async def _create_grant(
    input: GrantsCreateInput, may_replace: Callable[[GrantState], None] | None
) -> datetime:
    """`grants.create`'s write and what follows it; returns when it was granted."""
    # A grant to a verified email is written under its account's login handle, so the
    # person keeps one row; any other spelling is written as typed. The downgrade
    # guard reads that same row.
    from server.auth.aliases import canonical_subject

    subject = await canonical_subject(input.org, input.subject)

    async def _write(_row: GrantState | None) -> datetime:
        return await _write_grant(input, subject, may_replace)

    if input.replace and input.role != "admin":
        # Only `replace=true` lowers a grant (else 409): it may take admin.
        now = await _taking_admin(input, subject, _write, lowered_to=input.role)
    else:
        async with grant_change(input.org, input.namespace, subject):
            now = await _write_grant(input, subject, may_replace)
    if input.role == "admin":
        # A maintainer maintains every branch of the repository.
        from server.code.app.index import maintainers

        await maintainers.sync_quietly(input.org, input.namespace)
    return now


async def _write_grant(
    input: GrantsCreateInput,
    subject: str,
    may_replace: Callable[[GrantState], None] | None = None,
) -> datetime:
    """`grants.create`'s read of the current grant and its write, under the
    grant's claim."""
    existing = await _refuse_accidental_downgrade(input, subject, may_replace)
    if existing is not None and _active_admin(existing) and input.role != "admin":
        await _refuse_last_admin(input)
    pending_role = existing.pending_role if existing is not None else None
    pending_since = existing.pending_since if existing is not None else None
    if pending_role is not None and grant_rank(input.role) >= grant_rank(str(pending_role)):
        pending_role, pending_since = None, None
    now = datetime.now(UTC).replace(tzinfo=None)
    # granted_by is who issued the grant — the authenticated caller, as
    # grants.approve records the approver. The body's value is honoured only
    # with auth off, where there is no caller to stamp.
    from server.auth.context import current_auth

    auth = current_auth()
    granted_by = auth.subject if auth is not None and auth.subject else input.granted_by or "admin"
    await grants_repo.insert_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": subject,
            "role": input.role,
            "status": "active",
            "pending_role": pending_role,
            "pending_since": pending_since,
            "granted_at": now,
            "granted_by": granted_by,
            "revoked_at": None,
            "version": _version(),
        }
    )
    return now


async def handle_grants_list(input: GrantsListInput) -> GrantsListOutput:
    """Page through live grants after folding each person's spellings."""
    scope = ("grants.list", input.org, input.namespace)
    after = decode_bound_cursor(input.cursor, scope) if input.cursor else None
    people = await grants_repo.people_page(
        input.org, input.namespace, after=after, limit=input.limit + 1
    )
    page, truncated = people[: input.limit], len(people) > input.limit
    entries = [
        _GrantEntry(
            subject=p.subject,
            also=list(p.also),
            role=p.role,
            status=p.status,
            pending_role=p.pending_role,
            pending_since=p.pending_since,
            granted_at=p.granted_at,
            granted_by=p.granted_by,
        )
        for p in page
    ]
    project_rows(entries, _GrantEntry, input.fields, always=frozenset({"subject"}))
    next_cursor = None
    if truncated:
        next_cursor = encode_bound_cursor(scope, page[-1].granted_at, page[-1].subject)
    return GrantsListOutput(rows=entries, next_cursor=next_cursor, truncated=truncated)


async def handle_grants_pending_for_me(
    input: GrantsPendingForMeInput,
) -> GrantsPendingForMeOutput:
    """Every pending request in the namespaces the caller administers, in one statement. The
    caller is the request's principal (all spellings); their own requests are not
    approvals."""
    from server.auth.aliases import subject_aliases
    from server.auth.context import current_auth

    auth = current_auth()
    subject = str(getattr(auth, "subject", "") or "")
    if not subject:
        return GrantsPendingForMeOutput(rows=[])
    subjects = await subject_aliases(input.org, subject)
    rows = await grants_repo.pending_for_admins(input.org, subjects, limit=input.limit)
    return GrantsPendingForMeOutput(
        rows=[
            _PendingForMeEntry(
                namespace=r.namespace,
                kind=r.kind,
                subject=r.subject,
                current_role=r.role,
                requested_role=r.pending_role or r.role,
                pending_since=r.pending_since or r.granted_at,
            )
            for r in rows
        ]
    )


async def _live_grant_rows(org: str, namespace: str, subject: str) -> list[tuple[str, GrantState]]:
    """The (stored subject, row) pairs a revoke of `subject` takes: the live row
    under every spelling the gate admits the person by (the subject, its login
    handle, its verified email), matched in any case as the gate matches them,
    since one left live would keep admitting them. When the subject is an address
    that never held a row there, the rows under the spellings a revocation reaches
    (`aliases.spellings`): a grant for it stored under an unverified account's
    handle. A handle never falls back to its unverified email, which may name
    someone else's grant."""
    from server.auth.aliases import spellings, verified_spellings

    verified = await verified_spellings(org, subject)
    found = await _rows_under(org, namespace, verified)
    if found or "@" not in subject:
        return found
    if await grants_repo.stored_spellings(org, namespace, verified, live=False):
        return []  # revoked already: a retry takes nothing more
    seen = {s.lower() for s in verified}
    others = [s for s in await spellings(org, subject) if s.lower() not in seen]
    return await _rows_under(org, namespace, others)


async def _rows_under(
    org: str, namespace: str, spellings: list[str]
) -> list[tuple[str, GrantState]]:
    """Each live row on (org, namespace) stored under one of `spellings`, in any case,
    with the subject it is stored under."""
    found: list[tuple[str, GrantState]] = []
    for stored in await grants_repo.stored_spellings(org, namespace, spellings):
        row = await grants_repo.find_any_grant(org, namespace, stored)
        if row is not None:
            found.append((stored, row))
    return found


async def handle_grants_revoke(input: GrantsRevokeInput) -> GrantsRevokeOutput:
    """Mark a grant revoked (revoking a pending request = reject). Idempotent.

    Refuses to revoke a namespace's last active admin — that would leave it
    ownerless; promote a replacement first.
    """
    return await revoke_grant(input)


async def revoke_grant(
    input: GrantsRevokeInput, may_take: Callable[[GrantState], None] | None = None
) -> GrantsRevokeOutput:
    """`grants.revoke`; `may_take` raises for a row this caller must not
    revoke, checked before any change and again on the row read under the
    grant's claim. Every live row of the person's (`_live_grant_rows`) goes."""
    rows = await _live_grant_rows(input.org, input.namespace, input.subject)
    if may_take is not None:
        for _, row in rows:
            may_take(row)
    # Refused before any write when the person holds every admin row there. Each
    # row's write checks again under the admins claim, so an admin revoked
    # meanwhile can still stop a later row: the earlier ones stay revoked, and
    # the namespace keeps an admin.
    admins = sum(_active_admin(row) for _, row in rows)
    if admins:
        await _refuse_last_admin(input, also_going=admins - 1)
    revoked_at = None
    for subject, row in rows:
        now = await _revoke_row(input, subject, row, may_take)
        revoked_at = now or revoked_at
    if revoked_at is None:
        return GrantsRevokeOutput(revoked=False)
    return GrantsRevokeOutput(revoked=True, revoked_at=revoked_at)


async def _revoke_row(
    input: GrantsRevokeInput,
    subject: str,
    row: GrantState,
    may_take: Callable[[GrantState], None] | None,
) -> datetime | None:
    """Revoke the row stored under `subject`, under its claim, and the grants of the
    tokens it minted there; when it was revoked, or None when nothing was."""

    async def _write(row: GrantState | None) -> tuple[GrantState, datetime] | None:
        if row is None:
            return None
        if may_take is not None:
            may_take(row)
        return row, await _write_revoke(input, subject, row)

    done = None
    latest: GrantState | None = row
    if not _active_admin(row):
        # A reader's grant, or a request: only its own claim, unless it became admin.
        async with grant_change(input.org, input.namespace, subject):
            latest = await grants_repo.find_any_grant(input.org, input.namespace, subject)
            if latest is None or not _active_admin(latest):
                done = await _write(latest)
    if done is None and latest is not None and _active_admin(latest):
        done = await _taking_admin(input, subject, _write)
    if done is None:
        return None
    row, now = done
    # The tokens this person minted lose their own grants here. Best-effort: the
    # person's revoke has landed; a failed cascade is logged, never a 500.
    from server.identity.app import token_grants

    try:
        await token_grants.cascade_revoke_to_created_tokens(
            input.org, input.namespace, subject, named=input.subject
        )
    except Exception as e:  # noqa: BLE001 - the revoke itself succeeded
        log.warning(
            "token grant cascade failed for %s/%s %s: %s",
            input.org,
            input.namespace,
            subject,
            e,
        )
    return now


async def _taking_admin[T](
    input: GrantsRevokeInput | GrantsCreateInput,
    subject: str,
    write: Callable[[GrantState | None], Awaitable[T]],
    *,
    lowered_to: str | None = None,
) -> T:
    """A change that may take admin from `subject` (a revoke, or a lowering to
    `lowered_to`), under the repository's and the namespace's admins claims: a
    maintainer is first taken off the other branches, so when one fails the
    grant is still live and a retry redoes it all; then `write` runs under the
    grant's own claim with the grant as read there."""
    from server.code.app.index import maintainers

    org, namespace = input.org, input.namespace
    async with (
        maintainers.repo_change(org, namespace, subject),
        maintainers.repo_admins_change(org, namespace),
        admins_change(org, namespace),
    ):
        fanned_out = False
        for _ in range(_REVOKE_TRIES):
            row = await grants_repo.find_any_grant(org, namespace, subject)
            if row is not None and _active_admin(row) and not fanned_out:
                await _refuse_last_admin(input)
                await maintainers.revoke_repo_maintainer(
                    org, namespace, subject, lowered_to=lowered_to
                )
                fanned_out = True
            async with grant_change(org, namespace, subject):
                # Read again under the claim: what was read before it may have changed.
                row = await grants_repo.find_any_grant(org, namespace, subject)
                if row is not None and _active_admin(row) and not fanned_out:
                    continue  # made admin since the first read: fan out first
                return await write(row)
    raise GrantBusyError(f"{input.subject!r} on {org}/{namespace} keeps changing; retry shortly")


def _active_admin(row: GrantState) -> bool:
    # Only an active admin row is load-bearing for ownership.
    return row.role == "admin" and (row.status or "active") == "active"


async def _refuse_last_admin(
    input: GrantsRevokeInput | GrantsCreateInput, *, also_going: int = 0
) -> None:
    """Refuse a change that would leave the namespace without an admin, with
    `also_going` more of its admin rows taken by the same change; it is counted
    under `admins_change`."""
    if await grants_repo.count_active_admins(input.org, input.namespace) <= 1 + also_going:
        from server.core.errors import PreconditionFailedError

        raise PreconditionFailedError(
            f"{input.subject!r} is the only admin of "
            f"{input.namespace!r}; promote another member to admin "
            f"first (grants.create role=admin)"
        )


async def _write_revoke(input: GrantsRevokeInput, subject: str, row: GrantState) -> datetime:
    """`grants.revoke`'s write, under the grant's claim. Refuses to revoke a
    namespace's last active admin."""
    if _active_admin(row):
        await _refuse_last_admin(input)
    now = datetime.now(UTC).replace(tzinfo=None)
    await grants_repo.insert_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": subject,
            "role": row.role,
            "status": row.status or "active",
            "granted_at": now,
            "granted_by": row.granted_by,
            "revoked_at": now,
            "version": _version(),
        }
    )
    return now


async def handle_grants_approve(input: GrantsApproveInput) -> GrantsApproveOutput:
    """Approve or reject a pending grant. No-op if it doesn't exist."""
    _enforce_principal_target(input.namespace, input.subject)
    async with grant_change(input.org, input.namespace, input.subject):
        out = await _decide_request(input)
    if out.decision == "approve" and out.role == "admin":
        # An approved maintainer maintains every branch.
        from server.code.app.index import maintainers

        await maintainers.sync_quietly(input.org, input.namespace)
    return out


async def _decide_request(input: GrantsApproveInput) -> GrantsApproveOutput:
    """`grants.approve`'s read of the request and its write, under the grant's
    claim: a promotion or revoke landing after the read would otherwise be
    written over."""
    row = await grants_repo.find_any_grant(
        input.org,
        input.namespace,
        input.subject,
    )
    # Only a request is decided: an active grant with no pending elevation has nothing to
    # approve, and approving it with `role` would rewrite a live grant past the downgrade and
    # last-admin guards grants.create / grants.revoke apply.
    if row is None or (row.status != "pending" and not row.pending_role):
        # Raced with a concurrent revoke/approve, or nothing pending — no-op, no role.
        return GrantsApproveOutput(
            subject=input.subject,
            namespace=input.namespace,
            decision=input.decision,
            role=None,
            decided_at=datetime.now(UTC).replace(tzinfo=None),
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    # Two pending shapes from namespace.join:
    #   no base access: status=pending, role IS the request.
    #   has base access: status=active, pending_role carries the elevation.
    pending_role = row.pending_role
    no_base_access = row.status == "pending"
    # The approver may answer a request with a different role ("asked for maintainer,
    # approved as reader"): `input.role` overrides what was asked; absent, the request is
    # approved as asked.
    target_role = input.role or pending_role or row.role
    if input.decision == "approve" and not no_base_access:
        _refuse_approval_below(row.role, str(target_role), input)
    await _enforce_code_ceiling(input.org, input.namespace, str(target_role))
    # granted_by on the new row is the approver, not the requester — the
    # audit trail and the "approved by …" notification need the right person.
    from server.auth.context import current_auth

    auth = current_auth()
    approver = auth.subject if auth is not None and auth.subject else row.granted_by or "admin"
    if input.decision == "approve":
        await grants_repo.insert_row(
            {
                "org": input.org,
                "namespace": input.namespace,
                "subject": input.subject,
                "role": target_role,
                "status": "active",
                "pending_role": None,
                "pending_since": None,
                "granted_at": now,
                "granted_by": approver,
                "revoked_at": None,
                "version": _version(),
            }
        )
    else:
        if no_base_access:
            # No base access → reject tombstones the whole row.
            await grants_repo.insert_row(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "subject": input.subject,
                    "role": row.role,
                    "status": row.status or "pending",
                    "pending_role": None,
                    "pending_since": None,
                    "granted_at": now,
                    "granted_by": row.granted_by,
                    "revoked_at": now,
                    "version": _version(),
                }
            )
        else:
            # Has base access → reject clears only the request, leaving the
            # base role intact so the user isn't silently demoted.
            await grants_repo.insert_row(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "subject": input.subject,
                    "role": row.role,
                    "status": "active",
                    "pending_role": None,
                    "pending_since": None,
                    "granted_at": now,
                    "granted_by": row.granted_by,
                    "revoked_at": None,
                    "version": _version(),
                }
            )
    return GrantsApproveOutput(
        subject=input.subject,
        namespace=input.namespace,
        decision=input.decision,
        role=target_role,
        decided_at=now,
    )


async def revoke_subject(org: str, subject: str, *, actor: str) -> int:
    """Revoke every live grant naming `subject` in `org` (a deleted group, an
    erased user), each under its grant's claim and audited as `grants.revoke`,
    from the row read under that claim and versioned above it whatever this
    host's clock says. Reads again until none is live: a grant written
    meanwhile is revoked too, and one that still stands fails the call (503)
    for its caller to retry. Moves the watermark once. Returns how many."""
    from server.core.errors import BackendUnavailableError
    from server.observability import audit

    revoked = 0
    attempted = False  # a write that landed but whose reply was lost still announces
    try:
        for _ in range(_REVOKE_TRIES):
            listed = await grants_repo.live_rows_of(org, subject)
            if not listed:
                return revoked
            for namespace in sorted({str(r["namespace"]) for r in listed}):
                async with grant_change(org, namespace, subject):
                    row = await grants_repo.newest_row(org, namespace, subject)
                    if row is None or row["revoked_at"] is not None:
                        continue  # revoked meanwhile
                    attempted = True
                    try:
                        await grants_repo.insert_row(
                            {**row, "revoked_at": _now(), "version": _above(row)}, announce=False
                        )
                    finally:
                        # Audited even when the reply is lost: the row may have
                        # landed, and the retry would find nothing to audit.
                        await audit.emit(
                            "grants.revoke",
                            org=org,
                            namespace=namespace,
                            actor=actor,
                            subject={"subject": subject},
                        )
                revoked += 1
        if not await grants_repo.live_rows_of(org, subject):
            return revoked  # the last pass revoked the last of them
        raise BackendUnavailableError(
            f"grants of {subject!r} are still live after their revoke; retry",
            operation="grants.revoke",
        )
    finally:
        if attempted:
            await grant_cache.changed(org, [subject])


async def forget_granter(org: str, granter: str) -> int:
    """Blank `granter` as the issuer of the grants it gave others (an erased
    user), each under its grant's claim from the row read there (a revoke or a
    downgrade that landed meanwhile is kept) and versioned above it. The grants
    themselves stay: they are the grantees'. Moves the watermark once. Returns
    how many rows it rewrote."""
    rewritten = 0
    attempted: set[str] = set()  # announced even when a write's reply was lost
    try:
        for listed in await grants_repo.rows_granted_by(org, granter):
            namespace, subject = str(listed["namespace"]), str(listed["subject"])
            async with grant_change(org, namespace, subject):
                row = await grants_repo.newest_row(org, namespace, subject)
                if row is None or row["granted_by"] != granter:
                    continue  # rewritten meanwhile by someone else's grant
                attempted.add(subject)
                await grants_repo.insert_row(
                    {**row, "granted_by": "", "version": _above(row)}, announce=False
                )
            rewritten += 1
    finally:
        if attempted:
            await grant_cache.changed(org, sorted(attempted))
    return rewritten


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _above(row: dict[str, Any]) -> int:
    """A version above `row`'s whatever this host's clock says."""
    return max(_version(), int(row["version"]) + 1)
