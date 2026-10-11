"""One delegation policy for every token minted from another token.

`/v1/me/setup/mcp_config`, `/v1/me/service_accounts.create` and
`/v1/admin/token.mint` call `delegate`. A child gets

    requested ∩ the parent token's ceiling ∩ the creator's live grants

and never more: same org, grant roles only (org_admin / platform_admin are
never delegated), the parent's read scope and write namespaces, an expiry no
later than the parent's, and the parent's chain plus the parent's own id.
The CLI `loom mint-token` holds the signing secret and is not a delegation.

The creator is the root principal of the chain (a grandchild keeps it).
Live grants are rechecked on every request, inside the token's own claim
(`authz.check_authorized`, `context.resolve_my_namespaces`), and
`jwt.verify_token` refuses the child once any ancestor token or the
creator is revoked.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException

from server.auth import authz
from server.auth import jwt as jwt_mod
from server.auth.authz import grant_role
from server.auth.jwt import AuthInfo
from server.operations.base import Role
from server.settings import settings

log = logging.getLogger(__name__)

# A delegated token without an explicit TTL lives this long.
DEFAULT_TTL_DAYS = 90
# The longest parent chain a token may carry.
MAX_CHAIN_DEPTH = 8


@dataclass(frozen=True)
class Delegated:
    """What `delegate` allows: the claims to mint the child with."""

    org: str
    namespaces: list[str]
    roles: list[str]
    ns_roles: dict[str, str] | None
    write_namespaces: list[str] | None
    read_scope: str
    read_user_id: str
    ttl_seconds: int
    parents: list[str]
    creator: str
    is_service_account: bool

    def mint(self, subject: str, *, jti: str | None = None, consumer: str | None = None) -> str:
        return jwt_mod.mint_token(
            subject=subject,
            org=self.org,
            namespaces=self.namespaces,
            roles=self.roles,
            write_namespaces=self.write_namespaces,
            ns_roles=self.ns_roles,
            ttl_seconds=self.ttl_seconds,
            is_service_account=self.is_service_account,
            read_scope=self.read_scope or None,
            read_user_id=self.read_user_id or None,
            consumer=consumer,
            jti=jti,
            parents=self.parents,
            creator=self.creator,
        )


def _refuse(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": "AUTH_INSUFFICIENT", "message": message})


async def delegate(
    parent: AuthInfo,
    *,
    org: str,
    requested: dict[str, str | None],
    scoped: bool,
    ttl_seconds: int | None,
    is_service_account: bool,
) -> Delegated:
    """The child `parent` may mint in `org`. `requested` maps each namespace,
    in read-priority order, to the role asked for there; None asks for the
    highest the parent and the creator allow. `"*"` (flat only) asks for
    whatever the creator holds live. `scoped` mints an `ns_roles` token;
    otherwise a flat token whose one role is the lowest role allowed on any
    requested namespace. Raises 403 AUTH_INSUFFICIENT naming what was refused."""
    child_org = _child_org(parent, org)
    # A child is never less of a service account than its parent.
    is_service_account = is_service_account or parent.is_service_account
    parents = [*parent.parents, parent.token_id] if parent.token_id else list(parent.parents)
    if len(parents) > MAX_CHAIN_DEPTH:
        raise _refuse(f"delegation chain is limited to {MAX_CHAIN_DEPTH} tokens")
    if scoped and "*" in requested:
        raise _refuse("a per-namespace token cannot name '*'")
    creator = parent.creator or parent.subject
    roles = await _allowed_roles(parent, child_org, creator, requested)
    if scoped:
        ns_roles: dict[str, str] | None = {ns: r.name.lower() for ns, r in roles.items()}
        flat: list[str] = []
    else:
        ns_roles = None
        flat = [min(roles.values()).name.lower()]
    return Delegated(
        org=child_org,
        namespaces=list(roles),
        roles=flat,
        ns_roles=ns_roles,
        write_namespaces=list(parent.write_namespaces) if parent.write_namespaces else None,
        read_scope=parent.read_scope,
        read_user_id=parent.read_user_id,
        ttl_seconds=_child_ttl(parent, ttl_seconds),
        parents=parents,
        creator=creator,
        is_service_account=is_service_account,
    )


def _child_org(parent: AuthInfo, org: str) -> str:
    """A child lives in its parent's org; an org-wide (`*`) parent names one."""
    if parent.org not in ("*", org):
        raise _refuse(f"cannot mint a token for org {org!r} from org {parent.org!r}")
    return org


def _child_ttl(parent: AuthInfo, ttl_seconds: int | None) -> int:
    """The child's TTL: the one asked for, else the default, clipped so the
    child never outlives its parent."""
    ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_DAYS * 86400
    if parent.expires_at is not None:
        left = parent.expires_at - int(time.time())
        if left <= 0:
            raise _refuse("the parent token has expired")
        ttl = min(ttl, left)
    return ttl


async def _allowed_roles(
    parent: AuthInfo, org: str, creator: str, requested: dict[str, str | None]
) -> dict[str, Role]:
    """The role the child gets on each namespace. A namespace the caller named
    and may not have is refused; one that came from expanding `*` over a
    narrow parent's list is left out instead."""
    expanded = _expand_wildcard(parent, requested)
    out: dict[str, Role] = {}
    for ns, asked in expanded.items():
        want = delegable_role(asked)
        allowed = ceiling_role(parent, org, ns)
        if ns != "*":
            allowed = _lower(allowed, await authz._grant_role_for(org, ns, creator))
        if allowed is None or (want is not None and want > allowed):
            if ns not in requested:
                continue
            raise _refuse(f"cannot delegate {want.name.lower() if want else 'access'} on {ns}")
        out[ns] = want or allowed
    if not out:
        raise _refuse("cannot delegate access to any of the requested namespaces")
    return out


def _expand_wildcard(parent: AuthInfo, requested: dict[str, str | None]) -> dict[str, str | None]:
    """`*` stays dynamic only under a parent whose own reach is dynamic: a
    primary flat token (login, OAuth) or a delegated token whose claim is
    `*`. Under a narrower parent it becomes that parent's namespace list."""
    if "*" not in requested:
        return requested
    narrow = parent.has_ns_roles() or (bool(parent.creator) and "*" not in parent.namespaces)
    if not narrow:
        return requested
    out = {ns: role for ns, role in requested.items() if ns != "*"}
    for ns in parent.namespaces:
        if ns != "*" and ns not in out:
            out[ns] = requested["*"]
    return out


def delegable_role(name: str | None) -> Role | None:
    """The grant role `name` asks for (None for None). 403 for an org-wide
    role, which is never delegated; 422 for a name that is no role."""
    if name is None:
        return None
    role = grant_role(name)
    if role is not None:
        return role
    if name.strip().upper() in Role.__members__:
        raise _refuse(f"{name.strip().lower()} is never delegated")
    raise HTTPException(
        status_code=422,
        detail={"code": "VALIDATION_FAILED", "message": f"unknown role {name!r}"},
    )


def _lower(a: Role | None, b: Role | None) -> Role | None:
    if a is None or b is None:
        return None
    return min(a, b)


def ceiling_role(token: AuthInfo, org: str, namespace: str, *, write: bool = False) -> Role | None:
    """The most `token` itself allows on `namespace`, at most admin. A scoped
    token's `ns_roles` entry; a delegated flat token's role where its claim
    covers the namespace (`write`: its write claim); a primary flat token's
    role anywhere, since it reads through its subject's grants by design."""
    if token.has_ns_roles():
        return token.effective_role(org, namespace)
    flat = max(token.roles, default=None)
    if flat is None:
        return None
    if token.creator:
        covered = token.can_write(org, namespace) if write else token.can_access(org, namespace)
        if not covered:
            return None
    return min(flat, Role.ADMIN)


# (org, creator) -> (time.monotonic() of the read, account active).
_account_active: dict[tuple[str, str], tuple[float, bool]] = {}


async def creator_account_active(org: str, creator: str) -> bool:
    """False when the creator owns namespaces in `org` and none is active any
    more (deactivated, claim-released): every token it delegated stops. A
    creator that owns none (an operator subject) is not disabled. Cached for
    the revocation refresh interval; a failed read counts as disabled."""
    from server.core.ch import get_client

    key = (org, creator)
    now = time.monotonic()
    hit = _account_active.get(key)
    if hit is not None and now - hit[0] < settings.jwt_revocation_cache_ttl_seconds:
        return hit[1]
    try:
        row = await get_client().query_one(
            """
            SELECT count() AS total, countIf(active = 1) AS live
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND owner_email = {sub:String}
            """,
            params={"org": org, "sub": creator},
        )
    except Exception as e:  # noqa: BLE001 — an unreadable account counts as disabled (closed)
        log.warning("account lookup failed for %s/%s: %s", org, creator, e)
        return False
    total = int((row or {}).get("total") or 0)
    live = int((row or {}).get("live") or 0)
    active = total == 0 or live > 0
    _account_active[key] = (now, active)
    return active


__all__ = [
    "DEFAULT_TTL_DAYS",
    "Delegated",
    "ceiling_role",
    "creator_account_active",
    "delegable_role",
    "delegate",
]
