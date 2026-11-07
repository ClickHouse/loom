"""Who a `/v1/me/*` caller is and what they hold.

These endpoints authenticate here, not through the operation registry's
role gate: `require_authed_caller` verifies the bearer, `caller_identity`
tolerates dev mode, and `caller_role_on` / `caller_admin_of` read the
caller's live grants under every spelling the directory ties to them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request

from server.core.grant_sql import LIVE_GRANT
from server.operations.base import grant_rank
from server.settings import settings

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo


async def caller_admin_of(org: str, namespace: str, subject: str) -> bool:
    """True if `subject` holds an active admin grant on `(org, namespace)`."""
    from server.core.ch import get_client

    # Any case, as the gate matches a grant's subject (`authz.grant_role_for`).
    subs = [s.lower() for s in await subject_aliases_of(org, subject)]
    row = await get_client().query_one(
        """
        SELECT 1 AS x FROM loom.namespace_grants FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND lower(subject) IN {subs:Array(String)}
          AND role = 'admin'
          AND {live}
        LIMIT 1
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "ns": namespace, "sub": subject, "subs": subs},
    )
    return row is not None


def parse_ns_roles(raw: object) -> dict[str, str]:
    """Decode the persisted `ns_roles` JSON map; empty/invalid -> {}."""
    if not raw:
        return {}
    import json as _json

    try:
        val = _json.loads(raw) if isinstance(raw, str) else raw
        return {str(k): str(v) for k, v in val.items()} if isinstance(val, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


async def caller_role_on(org: str, namespace: str, subject: str) -> str | None:
    """Return the caller's highest active role on (org, namespace) — one
    of "viewer" / "writer" / "admin", or None if no active grant. A pending
    join request (status='pending', role = the role asked for) is not one."""
    from server.core.ch import get_client
    from server.core.grant_sql import GRANT_RANK_SQL

    # Any case, as the gate matches a grant's subject (`authz.grant_role_for`).
    subs = [s.lower() for s in await subject_aliases_of(org, subject)]
    row = await get_client().query_one(
        """
        SELECT role
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String}
          AND namespace = {ns:String}
          AND lower(subject) IN {subs:Array(String)}
          AND {live}
        ORDER BY {rank} DESC
        LIMIT 1
        """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
        params={"org": org, "ns": namespace, "sub": subject, "subs": subs},
    )
    if row is None:
        return None
    r = str(row.get("role") or "")
    return r if grant_rank(r) else None


def require_authed_caller(request: Request) -> tuple[str, str]:
    """Return (subject, org) for the authenticated caller. Raises 401
    if no valid bearer. In dev mode (auth_required=false, no bearer),
    falls back to a body-supplied identity via the calling endpoint."""
    info = _authed(request)
    remember_caller(request, info.subject, info.org)
    return info.subject, info.org


def require_authed_info(request: Request) -> AuthInfo:
    """`require_authed_caller`, returning the verified token itself."""
    info = _authed(request)
    remember_caller(request, info.subject, info.org)
    return info


def _authed(request: Request) -> AuthInfo:
    """The verified bearer's principal; 401 when the request carries none."""
    from server.auth.bearer import missing_bearer, verify_bearer_header

    info = verify_bearer_header(request.headers.get("Authorization"))
    if info is None:
        raise missing_bearer()
    return info


def remember_caller(request: Request, subject: str, org: str) -> None:
    """The verified caller, for the ledger row the /v1/me middleware writes after the
    response."""
    try:
        request.state.loom_caller = (subject, org)
    except Exception:  # noqa: BLE001 — a bare test request may carry no state
        pass


async def subject_aliases_of(org: str, subject: str) -> list[str]:
    """Every spelling the directory ties to the caller (login handle + verified email), so a
    grant under either spelling counts."""
    from server.auth.aliases import subject_aliases

    return await subject_aliases(org, subject) if org and subject else [subject]


def caller_identity(request: Request, *, prefer_bearer_in_dev: bool = False) -> tuple[str, str]:
    """(subject, org) for a caller-API caller, tolerant of dev mode.

    Auth-on: verify the bearer (401 if missing/invalid). Auth-off: a synthetic
    single-user identity, unless a self-minted bearer is present and
    `prefer_bearer_in_dev` (so per-subject role lookups stay accurate on
    multi-member namespaces). A wildcard org falls back to self_signup_org.
    """
    from server.auth.bearer import bearer_token, gateway_refused
    from server.auth.gateway import is_end_user
    from server.auth.jwt import TokenError, verify_token

    if settings.auth_actually_required():
        info = _authed(request)
        org = info.org if info.org and info.org != "*" else settings.self_signup_org
        remember_caller(request, info.subject, org)
        return info.subject, org
    if prefer_bearer_in_dev:
        try:
            token = bearer_token(request.headers.get("Authorization"))
            if token is not None:
                info = verify_token(token)
                if is_end_user(info):
                    raise gateway_refused()
                org = info.org if info.org and info.org != "*" else settings.self_signup_org
                remember_caller(request, info.subject, org)
                return info.subject, org
        except TokenError:
            pass
    return "you", settings.self_signup_org


__all__ = [
    "caller_admin_of",
    "caller_identity",
    "caller_role_on",
    "parse_ns_roles",
    "remember_caller",
    "require_authed_caller",
    "subject_aliases_of",
]
