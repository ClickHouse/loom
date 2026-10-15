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

from server.auth.authz import Role
from server.auth.jwt import AuthInfo
from server.core.ch import CHClient, get_client
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
    row = dict(existing)
    if existing.get("contact_email"):
        # A person's account ties its login handle to its email, the binding that
        # caps a token's claims at the grants held under either; it is kept here.
        spellings = {(existing.get("owner_email") or "").lower(), existing["contact_email"].lower()}
        spellings.discard("")
        row["released_aliases"] = sorted(set(existing.get("released_aliases") or []) | spellings)
    row["owner_email"] = ""
    row["contact_email"] = ""
    row["password_hash"] = ""
    row["active"] = False
    row["description"] = f"Released by {actor_subject}" + (f" ({reason})" if reason else "")
    row["updated_at"] = max(now, existing.get("updated_at") or now)
    await ch.insert("loom.namespaces", [row])

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
