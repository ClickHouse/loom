"""grants.{create,list,revoke,approve} handlers."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from server.auth.revocation import write_grant_cutoffs
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


def _version() -> int:
    return time.time_ns()


def _actor() -> str:
    """Who is changing a grant: the authenticated caller; '' with auth off."""
    from server.auth.context import current_auth

    auth = current_auth()
    return auth.subject if auth is not None else ""


async def adopt_namespace(
    org: str, namespace: str, new_admin: str, *, actor: str
) -> datetime | None:
    """Install `new_admin` as admin on a namespace with no active admin grant
    (the recovery path for an orphaned namespace). Returns the grant time, or
    None, writing nothing, when the namespace still has an active admin."""
    from server.core.ch import get_client

    ch = get_client()
    if await grants_repo.count_active_admins(org, namespace, ch=ch) > 0:
        return None
    now = datetime.now(UTC).replace(tzinfo=None)
    await ch.insert(
        "loom.namespace_grants",
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
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return now


async def _enforce_code_ceiling(org: str, namespace: str, role: str) -> None:
    """On a code namespace `writer` does not exist; a reader (viewer) or maintainer (admin)
    may be granted or approved by anyone the transport's ADMIN gate let through: an org
    admin or a current maintainer of that repository."""
    from server.core.ch import get_client
    from server.core.errors import ValidationError
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


async def _refuse_accidental_downgrade(input: GrantsCreateInput, subject: str) -> GrantState | None:
    """The grants table replaces on (org, namespace, subject), so a `viewer` create for a
    current `admin` would lower them silently; this guard refuses that. Returns the
    subject's current active row (or None) so the write can carry
    its pending request forward; raises 409 when the requested role is below
    the current one and the caller did not say `replace=true`."""
    existing = await grants_repo.find_active_grant(input.org, input.namespace, subject)
    if existing is None:
        return None
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
    """Approving an elevation never lowers the base grant: lowering ends the
    claims of the person's older tokens, which `grants.create replace=true`
    does on purpose and an approval must not do on the side."""
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


async def handle_grants_create(input: GrantsCreateInput) -> GrantsCreateOutput:
    """Insert or raise a grant; lower one only on `replace=true`. Admin-issued grants are
    always active (the admin is the approver),
    so no approval step. A pending elevation the new role does not satisfy
    is carried forward; one it satisfies is answered by the grant."""

    await _enforce_code_ceiling(input.org, input.namespace, input.role)
    # A human's grant is written under the login handle, so one person keeps one row
    # whichever spelling the granter typed; the downgrade guard reads that same row.
    from server.auth.aliases import canonical_subject

    subject = await canonical_subject(input.org, input.subject)
    existing = await _refuse_accidental_downgrade(input, subject)
    pending_role = existing.pending_role if existing is not None else None
    pending_since = existing.pending_since if existing is not None else None
    if pending_role is not None and grant_rank(input.role) >= grant_rank(str(pending_role)):
        pending_role, pending_since = None, None
    if existing is not None and grant_rank(existing.role) > grant_rank(input.role):
        # Older tokens claim the role being taken away: a flat one falls back to
        # the new grant, a scoped one is capped at it.
        await write_grant_cutoffs(
            input.org,
            [input.namespace],
            [subject],
            reason="grants.create lowered",
            revoked_by=_actor(),
        )
    now = datetime.now(UTC).replace(tzinfo=None)
    # granted_by is who issued the grant — the authenticated caller, as
    # grants.approve records the approver. The body's value is honoured only
    # with auth off, where there is no caller to stamp.
    granted_by = _actor() or input.granted_by or "admin"
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
    if input.role == "admin":
        # A maintainer maintains every branch of the repository.
        from server.code.app.index import maintainers

        await maintainers.sync_quietly(input.org, input.namespace)
    return GrantsCreateOutput(
        org=input.org,
        namespace=input.namespace,
        subject=input.subject,
        role=input.role,
        granted_at=now,
    )


async def handle_grants_list(input: GrantsListInput) -> GrantsListOutput:
    """Live grants on one namespace — active and pending, revoked excluded."""

    rows = await grants_repo.list_active(
        input.org,
        input.namespace,
        limit=input.limit,
    )
    # One person is one row: a login handle and its email fold onto the first row seen, the
    # other spelling named in `also`.
    from server.auth.aliases import fold_by_alias, subject_aliases

    aliases_of = {}
    for r in rows:
        subj = str(r.get("subject") or "")
        if subj and subj not in aliases_of:
            aliases_of[subj] = await subject_aliases(input.org, subj)
    rows = fold_by_alias([dict(r) for r in rows], aliases_of)
    entries = [
        _GrantEntry(
            subject=r["subject"],
            also=list(r.get("also") or []),
            role=r["role"],
            status=r.get("status") or "active",
            pending_role=r.get("pending_role") or None,
            pending_since=r.get("pending_since"),
            granted_at=r["granted_at"],
            granted_by=r["granted_by"],
        )
        for r in rows
    ]
    project_rows(entries, _GrantEntry, input.fields, always=frozenset({"subject"}))
    return GrantsListOutput(rows=entries)


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


async def _live_grant_row(org: str, namespace: str, subject: str) -> tuple[str, GrantState | None]:
    """(the subject the grant row is stored under, that row): the login handle a human
    spelling canonicalises to, else the spelling as given."""
    from server.auth.aliases import canonical_subject

    canonical = await canonical_subject(org, subject)
    row = await grants_repo.find_any_grant(org, namespace, canonical)
    if row is None and canonical != subject:
        return subject, await grants_repo.find_any_grant(org, namespace, subject)
    return canonical, row


async def handle_grants_revoke(input: GrantsRevokeInput) -> GrantsRevokeOutput:
    """Mark a grant revoked (revoking a pending request = reject). Idempotent.

    Refuses to revoke a namespace's last active admin — that would leave it
    ownerless; promote a replacement first. The cutoffs that end older tokens'
    claims, here and on a maintainer's sibling branches, are written before
    any grant row, so a failed cutoff write fails the revoke. A sibling's grant
    row that then fails to land is logged and left for an operator to revoke.
    """

    subject, row = await _live_grant_row(input.org, input.namespace, input.subject)
    if row is None:
        return GrantsRevokeOutput(revoked=False)

    active = (row.status or "active") == "active"
    # Only an active admin row is load-bearing for ownership.
    if row.role == "admin" and active:
        if await grants_repo.count_active_admins(input.org, input.namespace) <= 1:
            from server.core.errors import PreconditionFailedError

            raise PreconditionFailedError(
                f"{input.subject!r} is the only admin of "
                f"{input.namespace!r}; promote another member to admin "
                f"first (grants.create role=admin)"
            )

    from server.code.app.index import maintainers

    siblings: list[dict[str, Any]] = []
    if row.role == "admin" and active:
        # A revoked maintainer leaves every branch of the repository: planned
        # first, so one cutoff write covers them all before any grant row lands.
        siblings = await maintainers.plan_revoke(input.org, input.namespace, subject)
    if active:
        # A pending request granted nothing, so revoking it (a reject) ends no claim.
        await write_grant_cutoffs(
            input.org,
            [input.namespace, *(r["namespace"] for r in siblings)],
            [subject],
            reason="grants.revoke",
            revoked_by=_actor(),
        )
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
    if siblings:
        await maintainers.write_revokes(input.org, input.namespace, subject, siblings)
    # The tokens this person minted lose their own grants here. Best-effort like the
    # maintainer fan-out above: the person's revoke has landed; a failed cascade is logged,
    # never a 500.
    from server.identity.app import token_grants

    try:
        await token_grants.cascade_revoke_to_created_tokens(input.org, input.namespace, subject)
    except Exception as e:  # noqa: BLE001 - the revoke itself succeeded
        log.warning(
            "token grant cascade failed for %s/%s %s: %s",
            input.org,
            input.namespace,
            subject,
            e,
        )
    return GrantsRevokeOutput(revoked=True, revoked_at=now)


async def handle_grants_approve(input: GrantsApproveInput) -> GrantsApproveOutput:
    """Approve or reject a pending grant. No-op if it doesn't exist."""

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
    approver = _actor() or row.granted_by or "admin"
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
        if str(target_role) == "admin":
            # An approved maintainer maintains every branch.
            from server.code.app.index import maintainers

            await maintainers.sync_quietly(input.org, input.namespace)
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
