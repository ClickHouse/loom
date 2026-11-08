"""One delegation policy for every token minted from another token.

A child gets requested ∩ the parent token's ceiling ∩ the creator's live
grants, never more; for a per-namespace child of a primary flat parent the
ceiling on a standard namespace is the grant itself (`_primary`). The rules
and the recheck on every request are in docs/guides/tokens-and-grants.md
(Delegated tokens). `loom mint-token` holds the signing secret and is not a
delegation.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import HTTPException

from server.auth import authz
from server.auth import jwt as jwt_mod
from server.auth.authz import grant_role
from server.auth.fail_closed import note_unreadable, unreadable
from server.auth.jwt import AuthInfo, TokenError
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.observability.metrics import record_auth_refusal
from server.operations.base import GRANT_ROLES, Role, grant_rank
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
    ttl_seconds: int | None  # None: never expires
    parents: list[str]
    parent_subjects: list[str]
    parent_iats: list[int]
    creator: str
    is_service_account: bool

    @property
    def ttl_days(self) -> int:
        """The TTL in whole days, rounded up; 0 for a token that never expires."""
        return math.ceil(self.ttl_seconds / 86400) if self.ttl_seconds else NEVER

    def expires_at(self, now: datetime) -> datetime | None:
        """When a token minted at `now` expires; None if never."""
        return None if self.ttl_seconds is None else now + timedelta(seconds=self.ttl_seconds)

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
            parent_subjects=self.parent_subjects,
            parent_iats=self.parent_iats,
            creator=self.creator,
        )

    def as_auth(self, subject: str) -> AuthInfo:
        """The identity a token minted for `subject` verifies as, without
        verifying it (no revocation list needed)."""
        return AuthInfo(
            subject=subject,
            org=self.org,
            namespaces=tuple(self.namespaces),
            write_namespaces=tuple(self.write_namespaces or ()),
            ns_roles=tuple((self.ns_roles or {}).items()),
            roles=tuple(Role[name.upper()] for name in self.roles),
            is_service_account=self.is_service_account,
            read_scope=self.read_scope,
            read_user_id=self.read_user_id,
            parents=tuple(self.parents),
            parent_subjects=tuple(self.parent_subjects),
            parent_iats=tuple(self.parent_iats),
            creator=self.creator,
        )


def _refuse(message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": "AUTH_INSUFFICIENT", "message": message})


async def _refuse_a_revoked_parent(parent: AuthInfo, issued_at: int, tokens: list[str]) -> None:
    """401 when ClickHouse says `parent` is revoked, judged as the gate judges it
    (`jwt._check_revoked`): this replica's snapshot may not have the revocation
    yet, and a child minted meanwhile would be dead at the gate but listed live,
    holding its name. `tokens`: its ancestor tokens and itself. A revocation
    landing between this read and the child's save leaves the child dead at the
    gate but listed live, a window of a few round trips."""
    from server.auth import revocation

    chain = zip(parent.parent_subjects, parent.parent_iats, strict=True)
    judged = [(parent.subject, issued_at), *chain]
    judged.append((parent.creator or parent.subject, min(iat for _, iat in judged)))
    cut = await revocation.cutoffs(sorted({s for s, _ in judged} | set(tokens)))
    by_subject = any(s in cut and revocation.kills(cut[s], iat) for s, iat in judged)
    # The sign-in session, first-party too (a logout ends it), owed revocations included.
    ended = bool(parent.session_id) and await revocation.revoked_outright(parent.session_id)
    if by_subject or any(t in cut for t in tokens) or ended:
        record_auth_refusal(TokenError(reason="revoked"), transport="http")
        raise HTTPException(
            status_code=401,
            detail={
                "code": "AUTH_INVALID",
                "message": "this token or one it was made from is revoked",
            },
        )


# Asks for a token that never expires: 0, in seconds and in days alike.
NEVER = 0


async def delegate(
    parent: AuthInfo,
    *,
    org: str,
    requested: dict[str, str | None],
    scoped: bool,
    ttl_seconds: int | None,
    is_service_account: bool,
    ends_with_session: bool = False,
) -> Delegated:
    """The child `parent` may mint in `org`. `requested` maps each namespace,
    in read-priority order, to the role asked for there; None asks for the
    highest the parent and the creator allow. `"*"` (flat only) asks for
    whatever the creator holds live. `scoped` mints an `ns_roles` token;
    otherwise a flat token whose one role is the lowest role allowed on any
    requested namespace, a role-less name beside `*` counting at the parent's
    ceiling (`_flat_role`). The caller lists the child where its owner can revoke
    it (`loom.service_accounts`). `ends_with_session` also names a first-party
    sign-in session among the child's `parents`, so the child ends at sign-out as
    a third-party client's always does (the assistant's per-answer token). Raises
    403 AUTH_INSUFFICIENT naming what was refused."""
    child_org = _child_org(parent, org)
    if parent.is_service_account and not is_service_account:
        raise _refuse("a service-account token can only mint service-account tokens")
    parents = [*parent.parents, parent.token_id] if parent.token_id else list(parent.parents)
    if parent.session_id and (ends_with_session or not parent.first_party):
        # A third-party client's session, or a first-party one asked for: what it
        # mints ends when the session is revoked, not when the access token runs out.
        parents.append(parent.session_id)
    if len(parents) > MAX_CHAIN_DEPTH:
        # Counts the ancestor tokens and, for a third-party client, its session.
        raise _refuse(f"a delegation chain is limited to {MAX_CHAIN_DEPTH} ancestors")
    if scoped and "*" in requested:
        raise _refuse("a per-namespace token cannot name '*'")
    if parent.issued_at is None:
        # The child's revocation check needs the parent's age (jwt.parent_iats).
        raise _refuse("the parent token carries no issue time")
    await _refuse_a_revoked_parent(parent, parent.issued_at, parents)
    creator = parent.creator or parent.subject
    roles = await _allowed_roles(parent, child_org, creator, requested, scoped)
    if scoped:
        ns_roles: dict[str, str] | None = {ns: r.name.lower() for ns, r in roles.items()}
        flat: list[str] = []
    else:
        ns_roles = None
        flat = [_flat_role(parent, child_org, requested, roles).name.lower()]
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
        parent_subjects=[*parent.parent_subjects, parent.subject],
        parent_iats=[*parent.parent_iats, parent.issued_at],
        creator=creator,
        is_service_account=is_service_account,
    )


def _flat_role(
    parent: AuthInfo, org: str, requested: dict[str, str | None], roles: dict[str, Role]
) -> Role:
    """The lowest role allowed on any named namespace; beside `*`, a role-less name
    counts at the parent's ceiling, since the creator's live grants cap each call
    (docs/guides/tokens-and-grants.md, Delegated tokens)."""
    if "*" not in requested:
        return min(roles.values())

    def counted(ns: str) -> Role:
        # A namespace `*` expanded into carries the role asked for on `*`.
        if requested.get(ns, requested["*"]) is not None:
            return roles[ns]
        ceiling = ceiling_role(parent, org, ns)
        assert ceiling is not None  # `_allowed_roles` dropped every namespace without one
        return ceiling

    flat = min(counted(ns) for ns in roles)
    # A role asked for on `*` also caps the names beside it that asked for none.
    star = delegable_role(requested["*"])
    return flat if star is None else min(flat, star)


def _child_org(parent: AuthInfo, org: str) -> str:
    """A child lives in its parent's org; an org-wide (`*`) parent names one."""
    if parent.org not in ("*", org):
        raise _refuse(f"cannot mint a token for org {org!r} from org {parent.org!r}")
    return org


def _child_ttl(parent: AuthInfo, ttl_seconds: int | None) -> int | None:
    """The child's TTL in seconds (None: never expires): the one asked for,
    else the default. A child never outlives its parent. A session of Loom's
    own sign-in (`first_party`: `loom login`, the dashboard) has an access
    token that lives an hour and the next refresh replaces it, so its
    children take what was asked: they are what the person made for agents
    and CI, outlive the session (a logout, a lost refresh answer), and are
    revoked one by one or with the person; one asked to end with the session
    (`ends_with_session`) still takes what was asked, and a sign-out ends it.
    A third-party MCP client's session
    gets no such reach: its children end with its access token, or sooner
    with the session (`delegate` names it in their `parents`)."""
    ttl = DEFAULT_TTL_DAYS * 86400 if ttl_seconds is None else ttl_seconds
    never = ttl == NEVER
    if (parent.session_id and parent.first_party) or parent.expires_at is None:
        return None if never else ttl
    left = parent.expires_at - int(time.time())
    if left <= 0:
        raise _refuse("the parent token has expired")
    if never:
        raise _refuse(
            "a token that expires cannot make one that never does; only a `loom login` "
            "or dashboard SSO session can"
        )
    return min(ttl, left)


async def _allowed_roles(
    parent: AuthInfo, org: str, creator: str, requested: dict[str, str | None], scoped: bool
) -> dict[str, Role]:
    """The role the child gets on each namespace. A namespace the caller named
    and may not have is refused; one that came from expanding `*` over a
    narrow parent's list is left out instead. A scoped child asking to write
    a namespace needs the parent to be able to write it; one naming no role
    there gets at most viewer."""
    expanded = _expand_wildcard(parent, requested)
    out: dict[str, Role] = {}
    for ns, asked in expanded.items():
        want = delegable_role(asked)
        writes = scoped and want is not None and want >= Role.WRITER
        allowed = ceiling_role(parent, org, ns, write=writes)
        if ns != "*":
            grant = await authz.grant_role_for(org, ns, creator)
            if scoped and _primary(parent) and not await authz.may_be_restricted(org, ns):
                allowed = grant
            else:
                # A flat child keeps the parent's cap: its one role also
                # answers ops that name no namespace.
                allowed = _lower(allowed, grant)
        if allowed is None or (want is not None and want > allowed):
            if ns not in requested:
                continue
            raise _refuse(f"cannot delegate {want.name.lower() if want else 'access'} on {ns}")
        if scoped and want is None and ceiling_role(parent, org, ns, write=True) is None:
            # The parent can't write here, so neither can the child: say viewer.
            allowed = min(allowed, Role.VIEWER)
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


def _primary(token: AuthInfo) -> bool:
    """A flat token minted from no other: Loom's own sign-in, or
    `loom mint-token` with `--roles`. Its flat role is a platform role, not
    its role on a namespace: the gate admits it to a standard namespace on
    the subject's grant alone (`authz.check_authorized`), so a scoped child
    takes that grant there. A restricted namespace needs the role from the
    token too (`_check_restricted`), so the token role stays a cap there."""
    return not token.has_ns_roles() and not token.creator


def _lower(a: Role | None, b: Role | None) -> Role | None:
    if a is None or b is None:
        return None
    return min(a, b)


def ceiling_role(token: AuthInfo, org: str, namespace: str, *, write: bool = False) -> Role | None:
    """The most `token` itself allows on `namespace`, at most admin. A scoped
    token's `ns_roles` entry; a delegated flat token's role where its claim
    covers the namespace (`write`: its write claim); a primary flat token's
    role anywhere, since it reads through its subject's grants by design.
    A write also stays inside a scoped token's `ns_write` claim when it has one."""
    if token.has_ns_roles():
        if write and token.write_namespaces and not token.can_write(org, namespace):
            return None
        return token.effective_role(org, namespace)
    flat = max(token.roles, default=None)
    if flat is None:
        return None
    if token.creator:
        covered = token.can_write(org, namespace) if write else token.can_access(org, namespace)
        if not covered:
            return None
    return min(flat, Role.ADMIN)


@dataclass(frozen=True)
class _Account:
    """What the creator's rows in one org say about its delegated tokens."""

    active: bool
    best_grant: Role | None


# (org, creator) -> (time.monotonic() of the read, the account). Only the
# token's own org reaches it (authz checks the org first); the cap bounds it.
_account_active: dict[tuple[str, str], tuple[float, _Account]] = {}
_ACCOUNT_CACHE_MAX = 4096

_RANK_ROLE = {grant_rank(name): grant_role(name) for name in GRANT_ROLES}


async def creator_account_active(org: str, creator: str) -> bool:
    """False when the creator's account in `org` is disabled, so every token
    it delegated stops: it owns namespaces and none is active any more
    (deactivated), or it held grants and none is live (a claim release
    tombstones them and blanks the owner). A creator with neither (an
    operator subject) is not disabled. A failed read counts as disabled."""
    return (await _creator_account(org, creator)).active


async def creator_best_grant(org: str, creator: str) -> Role | None:
    """The highest live grant the creator holds on any namespace of `org`."""
    return (await _creator_account(org, creator)).best_grant


async def _creator_account(org: str, creator: str) -> _Account:
    """Cached for the revocation refresh interval; a failed read is not cached."""
    from server.auth.aliases import lowered, subject_aliases
    from server.core.ch import get_client

    key = (org, creator)
    now = time.monotonic()
    hit = _account_active.get(key)
    if hit is not None and now - hit[0] < settings.jwt_revocation_cache_ttl_seconds:
        return hit[1]
    try:
        subjects = await subject_aliases(org, creator)
        row = await get_client().query_one(
            f"""
            SELECT o.owned AS owned, o.owned_live AS owned_live,
                   g.granted AS granted, g.best_rank AS best_rank
            FROM (
                SELECT count() AS owned, countIf(active = 1) AS owned_live
                FROM loom.namespaces FINAL
                WHERE org = {{org:String}} AND lower(owner_email) IN {{subs:Array(String)}}
            ) AS o
            CROSS JOIN (
                SELECT countIf(status = 'active') AS granted,
                       maxIf({GRANT_RANK_SQL}, {LIVE_GRANT}) AS best_rank
                FROM loom.namespace_grants FINAL
                WHERE org = {{org:String}} AND lower(subject) IN {{subs:Array(String)}}
            ) AS g
            """,
            params={"org": org, "subs": lowered(subjects)},
        )
    except Exception as e:  # noqa: BLE001 — an unreadable account counts as disabled (closed)
        log.warning("account lookup failed for %s/%s: %s", org, creator, e)
        note_unreadable()
        return _Account(active=False, best_grant=None)
    account = _account_from_row(row or {})
    if unreadable():  # read with only some of the creator's spellings: not kept
        return account
    if len(_account_active) >= _ACCOUNT_CACHE_MAX:
        _account_active.clear()
    _account_active[key] = (now, account)
    return account


def _account_from_row(row: dict[str, Any]) -> _Account:
    counts = {k: int(row.get(k) or 0) for k in ("owned", "owned_live", "granted", "best_rank")}
    best = _RANK_ROLE.get(counts["best_rank"])
    owns_ok = counts["owned"] == 0 or counts["owned_live"] > 0
    grants_ok = counts["granted"] == 0 or best is not None
    return _Account(active=owns_ok and grants_ok, best_grant=best)


__all__ = [
    "DEFAULT_TTL_DAYS",
    "Delegated",
    "ceiling_role",
    "creator_account_active",
    "creator_best_grant",
    "delegable_role",
    "delegate",
]
