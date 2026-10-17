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

from server.auth import agents, machines, sso_links
from server.auth.authz import Role
from server.auth.jwt import AuthInfo
from server.core import grant_cache
from server.core.ch import CHClient, get_client
from server.identity.domain.account_ties import kept_aliases
from server.identity.store import grants as grants_repo

log = logging.getLogger(__name__)
router = APIRouter()


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
    """
    if ch is None:
        ch = get_client()
    from server.auth.grant_claims import account_change

    async with account_change(org, namespace):  # a password set rewrites the same row
        return await _release(org, namespace, reason, actor_subject, ch)


async def _release(
    org: str, namespace: str, reason: str, actor_subject: str, ch: CHClient
) -> ClaimReleaseOutput | None:
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

    # Clear owner and mark inactive. The row stays, so the name stays taken
    # (self-signup's first-claim check does not filter on `active`): the
    # namespace's data is still there, and a re-claim would hand it to a
    # stranger. Insert-as-replace carrying every other column
    # (classification, join_policy, kind, caps, ...) forward.
    # The contact email and password are cleared with the owner, so the old
    # credential dies with the claim.
    # Their tokens end first: the row and the bots below are what names them,
    # so a release stopped after rewriting them could not find them again.
    await _end_tokens(org, namespace, existing, actor_subject, ch)
    row = dict(existing)
    row["released_aliases"] = kept_aliases(existing)
    row["owner_email"] = ""
    row["contact_email"] = ""
    row["password_hash"] = ""
    row["active"] = False
    row["description"] = f"Released by {actor_subject}" + (f" ({reason})" if reason else "")
    row["updated_at"] = max(now, existing.get("updated_at") or now)
    await ch.insert("loom.namespaces", [row])
    await sso_links.forget_account(org, namespace, actor_subject, ch=ch)
    bots = await machines.forget_namespace(org, namespace, actor_subject, ch=ch)
    # Again, now the row and bots no longer name it: a bot token minted during
    # the first cutoffs is older than these.
    await _end_tokens(org, namespace, existing, actor_subject, ch, bots=bots)
    # The claim cap reads which accounts tie a person's spellings from this table.
    grant_cache.invalidate(org)

    log.info(
        "claim_release: %s released by %s (was owned by %r, %d grants revoked)",
        namespace,
        actor_subject,
        previous_owner,
        grants_count,
    )

    return ClaimReleaseOutput(
        released=True,
        namespace=namespace,
        previous_owner=previous_owner,
        grants_revoked=grants_count,
    )


async def _end_tokens(
    org: str,
    namespace: str,
    row: dict[str, Any],
    by: str,
    ch: CHClient,
    bots: list[str] | None = None,
) -> None:
    """The tokens of the bots naming the namespace (or of `bots`) end now; so
    do an agent's, when it is the agent's own namespace (its owner is the agent)."""
    from server.identity.app import account_admin

    if bots is None:
        bots = await machines.naming(org, namespace, ch=ch)
    ended = [machines.subject(name) for name in bots]
    owner = str(row.get("owner_email") or "")
    for t in row.get("tags") or []:
        trust = str(t).removeprefix(agents.TAG_PREFIX)
        if trust != str(t) and owner.startswith(agents.subject(trust, "")):
            ended.append(owner)  # the agent's own namespace: its owner is the agent
    for subject in filter(None, ended):
        await account_admin.end_tokens(subject, by=by, reason="namespace released")


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
