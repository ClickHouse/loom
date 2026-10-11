"""Service-account self-mint: `/v1/me/service_accounts{,.create,.revoke}`.

A namespace admin mints, lists and revokes scoped tokens for their own
namespaces, with no operator round-trip.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from server.auth.delegation import delegate
from server.auth.jwt import AuthInfo
from server.core.datetimes import naive_utc_now_seconds
from server.core.grant_sql import LIVE_GRANT
from server.identity.app import me_callers, token_grants
from server.operations.base import GRANT_ROLES, Role, grant_rank
from server.settings import settings

router = APIRouter()


def json_int(value: object) -> int:
    """`int(value)` for a JSON body value; TypeError for a list, object or null,
    as `int()` itself raises."""
    if not isinstance(value, (int, float, str)):
        raise TypeError(f"not a number: {type(value).__name__}")
    return int(value)


def _parse_sa_ttl(ttl_raw: object) -> int | None:
    """`ttl_days` of a scoped token in seconds: None = the delegation default
    (90 days); else 1..3650 days; anything else is a 422."""
    if ttl_raw is None:
        return None
    try:
        ttl_days = json_int(ttl_raw)
        if ttl_days < 1 or ttl_days > 3650:
            raise ValueError
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "ttl_days must be 1..3650 or null",
            },
        ) from e
    return ttl_days * 86400


def dev_parent(payload: dict[str, object]) -> AuthInfo:
    """The parent a token is delegated from in dev mode (no auth, no bearer): a
    flat writer for the body's subject and org."""
    return AuthInfo(
        subject=str(payload.get("subject") or "dev"),
        org=str(payload.get("org") or "default"),
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )


@router.post("/v1/me/service_accounts.create")
async def me_service_accounts_create(
    request: Request,
    payload: dict[str, object],
) -> dict[str, object]:
    """Mint a service-account JWT scoped to a chosen subset of the caller's
    namespaces, with a per-namespace role and an explicit priority order.

    Body:
      name: str          # human-friendly label (e.g. 'agent-detector')
      namespaces: [ ... ] # ORDERED (order = read priority). Each entry is
                          #   either a bare "namespace" string, or an object
                          #   {"namespace": str, "role"?: str}. Role per entry
                          #   defaults to the top-level `role`, else the
                          #   caller's live grant on that namespace.
      role: str|null      # default role for bare entries (viewer|writer|admin)
      ttl_days: int|null  # default and null: 90 days

    Each namespace's role is delegated from the caller's token
    (`server.auth.delegation.delegate`): at most the caller's token allows and
    the caller's live grant — the caller may DOWNGRADE (or keep) but never
    escalate. The token carries an `ns_roles` claim (authoritative
    per-namespace role) and an ordered `namespaces` claim (the read priority).
    Returns the JWT exactly once.
    """
    import hashlib
    import json as _json
    import time as _time

    from server.core.ch import get_client

    if settings.auth_actually_required():
        parent = me_callers.require_authed_info(request)
    else:
        parent = dev_parent(payload)
    subject, org = parent.subject, parent.org

    name = str(payload.get("name") or "").strip()
    if not name or not (1 <= len(name) <= 64):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "name required, 1-64 chars",
            },
        )

    raw_namespaces = payload.get("namespaces") or []
    if not isinstance(raw_namespaces, list) or not raw_namespaces:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "namespaces must be a non-empty array",
            },
        )

    default_role_raw = payload.get("role")
    if default_role_raw is not None and str(default_role_raw) not in GRANT_ROLES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "role must be viewer | writer | admin",
            },
        )
    default_role = str(default_role_raw) if default_role_raw is not None else None

    # Parse ordered entries (order = read priority). Each is a bare string or
    # {namespace, role?}. Duplicate namespaces collapse to first occurrence so
    # priority stays stable; the last-seen role wins for that namespace.
    ordered_ns: list[str] = []
    requested_role: dict[str, str | None] = {}
    for entry in raw_namespaces:
        if isinstance(entry, dict):
            ns = str(entry.get("namespace") or "").strip()
            r_raw = entry.get("role")
            r = str(r_raw) if r_raw is not None else default_role
        else:
            ns = str(entry).strip()
            r = default_role
        if not ns:
            continue
        if r is not None and r not in GRANT_ROLES:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_FAILED",
                    "message": f"role for {ns} must be viewer | writer | admin",
                },
            )
        if ns not in requested_role:
            ordered_ns.append(ns)
        requested_role[ns] = r
    if not ordered_ns:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "namespaces must be a non-empty array",
            },
        )

    delegated = await delegate(
        parent,
        org=org,
        requested=requested_role,
        scoped=True,
        ttl_seconds=_parse_sa_ttl(payload.get("ttl_days")),
        is_service_account=True,
    )
    ordered_ns = delegated.namespaces
    ns_roles = delegated.ns_roles or {}

    sa_hash = hashlib.sha1(
        f"{org}:{name}:{subject}:{_time.time_ns()}".encode(),
    ).hexdigest()[:6]
    sa_subject = f"sa:{name}-{sa_hash}"
    minted = delegated.mint(sa_subject)

    now = naive_utc_now_seconds()
    expires_at = now + timedelta(seconds=delegated.ttl_seconds)

    # `roles` column keeps the distinct effective roles for the list view;
    # `ns_roles` persists the authoritative per-namespace map (JSON).
    distinct_roles = sorted({r for r in ns_roles.values()}, key=grant_rank)

    ch = get_client()
    await ch.insert(
        "loom.service_accounts",
        [
            {
                "org": org,
                "name": name,
                "sa_subject": sa_subject,
                "namespaces": ordered_ns,
                "roles": distinct_roles,
                "ns_roles": _json.dumps(ns_roles),
                "ttl_days": delegated.ttl_days,
                "created_by": subject,
                "created_at": now,
                "expires_at": expires_at,
                "revoked_at": None,
                "revoked_by": "",
                "version": _time.time_ns(),
            }
        ],
    )
    # A restricted namespace ticked here is read only when the token's own subject holds a
    # direct grant — write it now, capped, revoked with the token; a name in the claim alone
    # would have every read of it refused.
    restricted_granted = await token_grants.grant_restricted_to_token(
        org, sa_subject, subject, ns_roles
    )

    return {
        "token": minted,
        "name": name,
        "sa_subject": sa_subject,
        "org": org,
        "namespaces": ordered_ns,
        "ns_roles": ns_roles,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "restricted_namespaces": restricted_granted,
    }


@router.get("/v1/me/service_accounts")
async def me_service_accounts_list(request: Request) -> dict[str, object]:
    """List SAs the caller can see: ones they created, or ones scoped
    to a namespace they hold admin on."""
    from server.core.ch import get_client

    if settings.auth_actually_required():
        subject, org = me_callers.require_authed_caller(request)
    else:
        subject = request.query_params.get("subject", "dev")
        org = request.query_params.get("org", "default")

    subs = await me_callers.subject_aliases_of(org, subject)
    rows = await get_client().query_all(
        """
        WITH admin_ns AS (
            SELECT namespace
            FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND subject IN {subs:Array(String)}
              AND role = 'admin'
              AND {live}
        ),
        sa_last_used AS (
            -- One row per subject, kept by a materialized view on the ledger:
            -- a few kB instead of a month of loom.agent_events,
            -- whose sort key does not carry the subject (~10 MB for this read).
            SELECT subject AS sa_subject, maxMerge(last_seen) AS last_used
            FROM loom.subject_last_seen
            WHERE org = {org:String}
              AND subject LIKE 'sa:%'
            GROUP BY subject
        )
        SELECT
            s.name AS name, s.sa_subject AS sa_subject,
            s.namespaces AS namespaces, s.roles AS roles,
            s.ttl_days AS ttl_days, s.created_by AS created_by,
            s.created_at AS created_at, s.expires_at AS expires_at,
            s.revoked_at AS revoked_at, s.revoked_by AS revoked_by,
            s.kind AS kind, s.client AS client, s.ns_roles AS ns_roles,
            lu.last_used AS last_used_at
        -- Filter + FINAL in a subquery, THEN join: CH 26.5's analyzer can't
        -- resolve a WHERE on a FINAL table that is also a JOIN side.
        FROM (
            SELECT name, sa_subject, namespaces, roles, ttl_days, created_by,
                   created_at, expires_at, revoked_at, revoked_by, kind, client,
                   ns_roles
            FROM loom.service_accounts FINAL
            WHERE org = {org:String}
              AND (
                  created_by = {sub:String}
                  OR arrayExists(n -> n IN (SELECT namespace FROM admin_ns), namespaces)
              )
            ORDER BY created_at DESC
            LIMIT 200
        ) AS s
        LEFT JOIN sa_last_used AS lu ON s.sa_subject = lu.sa_subject
        ORDER BY s.created_at DESC
        LIMIT 200
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "sub": subject, "subs": subs},
    )

    def _iso(v: datetime | None) -> str | None:
        return v.isoformat() if v else None

    return {
        "rows": [
            {
                "name": r["name"],
                "sa_subject": r["sa_subject"],
                "namespaces": r["namespaces"],
                "roles": r["roles"],
                "ttl_days": r["ttl_days"],
                "created_by": r["created_by"],
                "created_at": _iso(r.get("created_at")),
                "expires_at": _iso(r.get("expires_at")),
                "revoked_at": _iso(r.get("revoked_at")),
                "revoked_by": r.get("revoked_by") or "",
                "last_used_at": _iso(r.get("last_used_at")),
                "kind": r.get("kind") or "service_account",
                "client": r.get("client") or "",
                "ns_roles": me_callers.parse_ns_roles(r.get("ns_roles")),
            }
            for r in rows
        ],
    }


@router.post("/v1/me/service_accounts.revoke")
async def me_service_accounts_revoke(
    request: Request,
    payload: dict[str, object],
) -> dict[str, object]:
    """Mark an SA as revoked. The JWT itself remains valid until TTL —
    for hard cutoff, also revoke the grants the SA depends on. Caller
    must be the creator or admin on at least one of the SA's namespaces."""
    import time as _time

    from server.core.ch import get_client

    if settings.auth_actually_required():
        subject, org = me_callers.require_authed_caller(request)
    else:
        subject = str(payload.get("subject") or "dev")
        org = str(payload.get("org") or "default")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION_FAILED", "message": "name required"},
        )

    ch = get_client()
    row = await ch.query_one(
        """
        SELECT sa_subject, namespaces, created_by, created_at,
               expires_at, ttl_days, roles, kind, client,
               inherit_creator_grants, ns_roles
        FROM loom.service_accounts FINAL
        WHERE org = {org:String} AND name = {name:String}
        LIMIT 1
        """,
        params={"org": org, "name": name},
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NOT_FOUND",
                "message": f"service account {name!r} not found",
            },
        )

    authorized = row["created_by"] == subject
    if not authorized:
        for ns in row["namespaces"]:
            if await me_callers.caller_admin_of(org, ns, subject):
                authorized = True
                break
    if not authorized:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_INSUFFICIENT",
                "message": "must be the creator or admin on a scoped namespace",
            },
        )

    now = naive_utc_now_seconds()
    await ch.insert(
        "loom.service_accounts",
        [
            {
                "org": org,
                "name": name,
                "sa_subject": row["sa_subject"],
                "namespaces": row["namespaces"],
                "roles": row["roles"],
                "ttl_days": row["ttl_days"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "revoked_at": now,
                "revoked_by": subject,
                "version": _time.time_ns(),
                # Column-mirror discipline (CLAUDE.md): carry every column
                # forward or the merge zeros it.
                "kind": row.get("kind") or "service_account",
                "client": row.get("client") or "",
                "inherit_creator_grants": bool(row.get("inherit_creator_grants", True)),
                "ns_roles": row.get("ns_roles") or "{}",
            }
        ],
    )

    # Also revoke the JWT itself. SA tokens carry no `jti`, so revoke by
    # `sub` (the sa_subject); verify_token checks both. Without this the
    # token keeps authenticating until its TTL.
    from server.auth.revocation import revocation_expiry

    revocation_expires = revocation_expiry(row.get("expires_at"), now)
    await ch.insert(
        "loom.revoked_tokens",
        [
            {
                "jti": row["sa_subject"],
                "revoked_at": now,
                "expires_at": revocation_expires,
                "reason": f"service_account.revoke name={name}",
                "revoked_by": subject,
            }
        ],
    )
    # Refresh so this replica sees the revocation immediately; other
    # replicas pick it up on the next worker tick.
    from server.auth.revocation import refresh

    await refresh()
    # The token's own restricted grants die with it.
    grants_revoked = await token_grants.revoke_token_grants(org, str(row["sa_subject"]))

    return {
        "revoked": True,
        "name": name,
        "revoked_at": now.isoformat(),
        "grants_revoked": grants_revoked,
    }
