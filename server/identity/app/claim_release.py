"""Operator escape hatch to release a stuck namespace claim.

Revokes all grants, clears the owner, contact email and password, and marks
the row inactive. The
row stays, so the name stays reserved: a released namespace still holds its
memories, and letting a fresh self-signup claim the name would make that
signup admin over the previous owner's data. Erasing the namespace
(`erasure.request` scope `namespace`) removes the data; the registry row, and
with it the name, is kept even then.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.auth import agents, aliases, machines, revocation, sso_links
from server.auth.authz import Role
from server.auth.jwt import AuthInfo
from server.core import grant_cache
from server.core.ch import CHClient, get_client
from server.core.errors import BackendUnavailableError
from server.identity.app import account_admin
from server.identity.app.restricted_access import note_restricted_access
from server.identity.domain.account_ties import kept_aliases
from server.identity.store import grants as grants_repo

log = logging.getLogger(__name__)
router = APIRouter()

_REASON = "namespace released"


class ClaimReleaseInput(BaseModel):
    org: str
    namespace: Annotated[str, Field(min_length=1, max_length=128)]
    reason: str = ""


class ClaimReleaseOutput(BaseModel):
    released: bool
    namespace: str
    previous_owner: str
    grants_revoked: int


@router.post("/v1/admin/claim_release", response_model=ClaimReleaseOutput)
async def claim_release(
    input: ClaimReleaseInput,
    request: Request,
) -> ClaimReleaseOutput:
    """Release a self-signup claim: revoke its grants, clear the owner, deactivate it;
    the name stays reserved."""
    from server.insights.app.admin_routes import refuse_foreign_org, require_org_admin

    info = await require_org_admin(request)
    refuse_foreign_org(info, input.org, "release a claim in")
    actor_subject = info.subject if info is not None else "local"
    not_found = HTTPException(
        status_code=404,
        detail={
            "code": "NAMESPACE_NOT_FOUND",
            "message": f"{input.namespace!r} is not claimed in org {input.org!r}",
        },
    )
    # A restricted namespace's admins are its direct grantees, not the org's:
    # an org admin who could release it would take over what the restricted
    # gate withholds. Refused exactly like a missing name.
    if info is not None and not await _may_release(info, input.org, input.namespace):
        raise not_found
    await note_restricted_access(
        info, op="admin.claim_release", org=input.org, namespace=input.namespace, transport="http"
    )

    out = await release_claim(
        org=input.org,
        namespace=input.namespace,
        reason=input.reason,
        actor_subject=actor_subject,
    )
    if out is None:
        raise not_found
    return out


async def release_claim(
    *,
    org: str,
    namespace: str,
    reason: str,
    actor_subject: str,
    ch: CHClient | None = None,
) -> ClaimReleaseOutput | None:
    """Revoke every grant on (org, namespace), clear its owner and deactivate it.

    Writes version rows only (grant tombstones, a new loom.namespaces row); the
    registry row stays, so the name stays claimed. Returns None when the
    namespace has no registry row. The caller authorizes; `ch` defaults to the
    server's client.

    A release that fails must be retried: one cut off after its row was
    rewritten has not ended its bots' tokens, which keep the namespace until
    they expire (an hour, or up to a day for a bot that allows longer).
    """
    if ch is None:
        ch = get_client()
    from server.auth.grant_claims import account_change

    rewritten: list[bool] = []  # set once the loom.namespaces row is sent
    try:
        async with account_change(org, namespace):  # a password set rewrites the same row
            released = await _release(org, namespace, reason, actor_subject, ch, rewritten)
    except Exception as e:
        if rewritten:
            log.warning(
                "claim_release: %s/%s failed after its row was rewritten; "
                "retry it to end its bots' tokens: %s",
                org,
                namespace,
                e,
            )
        if isinstance(e, BackendUnavailableError):
            raise BackendUnavailableError(
                f"the release of {org}/{namespace} did not finish ({e.message}); retry it, "
                "or its bots' tokens keep the namespace until they expire",
                operation=e.extra.get("operation", ""),
            ) from e
        raise
    if released is None:
        return None
    out, cutoff = released
    if cutoff is not None:
        # Waited out like a bot replace or disable, so an exchange right after
        # is not refused as revoked; outside the claim, which is not held for it.
        await revocation.wait_out(cutoff)
    return out


async def _release(
    org: str, namespace: str, reason: str, actor_subject: str, ch: CHClient, rewritten: list[bool]
) -> tuple[ClaimReleaseOutput, datetime | None] | None:
    # SELECT * so the release row mirrors EVERY column forward: loom.namespaces is
    # ReplacingMergeTree(updated_at), and an omitted column would reset to its schema
    # default under FINAL (a restricted namespace would read back as standard).
    from server.identity.store import namespaces as namespaces_repo

    existing = await namespaces_repo.fetch_row(org, namespace, ch=ch)
    if existing is None:
        return None

    previous_owner = existing.get("owner_email", "") or ""

    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)

    # Revoke all grants by inserting tombstone rows.
    grants = await ch.query_all(
        """
        SELECT subject, role, granted_by FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
              AND revoked_at IS NULL
        """,
        params={"org": org, "ns": namespace},
    )
    grants_count = len({g["subject"] for g in grants})
    if grants:
        tombstones = [
            {
                "org": org,
                "namespace": namespace,
                "subject": g["subject"],
                "role": g["role"],
                "granted_at": now,
                "granted_by": g["granted_by"],
                "revoked_at": now,
                "version": time.time_ns() + i,
            }
            for i, g in enumerate(grants)
        ]
        await grants_repo.insert_rows(
            tombstones,
            ch=ch,
        )

    # The agent whose own namespace this is: cut off before its row is
    # rewritten, since the row is what names it. Its exchange makes and checks
    # the namespace under the claim held here, so its tokens predate this cutoff.
    agent = _agent_owner(existing)
    cutoff = (
        await account_admin.end_tokens(agent, by=actor_subject, reason=_REASON) if agent else None
    )
    # Read before the row is rewritten, and changed only by forget_namespace
    # below, so a retry after a failure in between still finds the bots.
    bots = await machines.naming(org, namespace, ch=ch)

    # Clear owner and mark inactive. The row stays, so the name stays taken
    # (self-signup's first-claim check does not filter on `active`): the
    # namespace's data is still there, and a re-claim would hand it to a
    # stranger. Insert-as-replace carrying every other column
    # (classification, join_policy, kind, caps, ...) forward.
    # The contact email and password are cleared with the owner, so the old
    # credential dies with the claim.
    row = dict(existing)
    row["released_aliases"] = kept_aliases(existing)
    row["owner_email"] = ""
    row["contact_email"] = ""
    row["password_hash"] = ""
    row["active"] = False
    row["description"] = f"Released by {actor_subject}" + (f" ({reason})" if reason else "")
    row["updated_at"] = max(now, existing.get("updated_at") or now)
    rewritten.append(True)
    await ch.insert("loom.namespaces", [row])

    # A bot's exchange reads its live namespaces sequentially, so no token
    # issued from here on carries this one: the bots' cutoff is taken now.
    for name in bots:
        cutoff = await account_admin.end_tokens(
            machines.subject(name), by=actor_subject, reason=_REASON
        )
    await sso_links.forget_account(org, namespace, actor_subject, ch=ch)
    await machines.forget_namespace(org, namespace, actor_subject, ch=ch)
    # The claim cap reads which accounts tie a person's spellings from this table.
    await grant_cache.changed(org)
    aliases.invalidate(org, [previous_owner, str(existing.get("contact_email") or "")])

    log.info(
        "claim_release: %s released by %s (was owned by %r, %d grants revoked)",
        namespace,
        actor_subject,
        previous_owner,
        grants_count,
    )

    out = ClaimReleaseOutput(
        released=True,
        namespace=namespace,
        previous_owner=previous_owner,
        grants_revoked=grants_count,
    )
    return out, cutoff


def _agent_owner(row: dict[str, Any]) -> str:
    """The agent owning `row`, when it is an agent's own namespace; else ''."""
    owner = str(row.get("owner_email") or "")
    for t in row.get("tags") or []:
        trust = str(t).removeprefix(agents.TAG_PREFIX)
        if trust != str(t) and owner.startswith(agents.subject(trust, "")):
            return owner
    return ""  # never a person's own sessions


async def _may_release(info: AuthInfo, org: str, namespace: str) -> bool:
    """False for a restricted namespace the caller holds no direct admin grant
    on; an unreadable directory counts as restricted."""
    from server.auth.authz import restricted_allows
    from server.auth.directory import ClassificationUnavailable
    from server.identity.app.namespace_classification import is_restricted

    try:
        restricted = await is_restricted(org, namespace)
    except ClassificationUnavailable:
        restricted = True
    return not restricted or await restricted_allows(info, Role.ADMIN, org, namespace)


def attach(app: Any) -> None:
    app.include_router(router)
