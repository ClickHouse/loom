"""Password-based sign-in: verify email-or-namespace + password, mint a JWT
with the user's full namespace set.

The namespace set is the namespaces the person owns (by contact email), less
those a grant of theirs was taken away on, plus every *restricted* namespace
they hold an active direct grant on. A restricted namespace is read only by a
token that names it, and the live grant is re-checked on every call, so naming
it here removes the "sign in with a different token" dead end for the
maintainers and approved readers of a restricted repository without
widening anything a revoked grant would still have to close. Standard
namespaces are not added: they authorize through the grant already, and a
name on a 365-day token would outlive the grant."""

from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.auth.attempts import Attempts, client_address
from server.auth.jwt import mint_token
from server.auth.passwords import hash_password, verify_password_off_loop
from server.core.ch import get_client

log = logging.getLogger(__name__)
router = APIRouter()

_JWT_TTL_SECONDS = 365 * 86400


# Credentials are `repr=False`: a model that reaches a log line or a
# traceback shows every field but these.
class LoginInput(BaseModel):
    # email or namespace name — looked up in that order
    identifier: Annotated[str, Field(min_length=1, max_length=320)]
    password: Annotated[str, Field(min_length=1, max_length=256, repr=False)]


class LoginOutput(BaseModel):
    token: str = Field(repr=False)
    subject: str
    org: str
    namespaces: list[str]
    expires_at: datetime


# Generic auth-failure message — never leak whether the account exists.
# A signed-in person: (subject, org, namespaces, roles).
Identity = tuple[str, str, list[str], list[str]]

_BAD_CREDS = {"code": "AUTH_INVALID", "message": "Invalid email or password."}

# Failed sign-ins per identifier (guessing one account's password) and per
# client address (spraying many accounts), per 15 minutes.
_failures_by_identifier = Attempts(10, 900)
_failures_by_address = Attempts(50, 900)


class LoginThrottled(Exception):
    """Too many failed sign-ins for this identifier or address; retry later."""


class SignInUnavailable(Exception):
    """The grant cutoffs a sign-in reads could not be read; retry later."""


@functools.cache
def _unknown_account_hash() -> str:
    """A real argon2id hash of a random secret, verified against when no
    account matches, so an unknown identifier costs one full hash like a
    wrong password does. A malformed placeholder would be rejected in
    microseconds, which tells an attacker which accounts exist."""
    return hash_password(secrets.token_urlsafe(24))


async def resolve_user_credentials(
    identifier: str, password: str, *, address: str = ""
) -> Identity | None:
    """Verify (email-or-namespace + password) → (subject, org, namespaces, roles).

    Shared by /v1/auth/login and the OAuth /authorize endpoint so the two can't
    drift on identity / namespace-set / admin-role resolution. Returns None on
    bad credentials (caller picks the error shape); raises LoginThrottled past
    the failure limit for the identifier or `address`. Always runs one
    password hash so response timing doesn't leak account existence.
    """
    ident = identifier.strip().lower()
    if _failures_by_identifier.blocked(ident) or (
        address and _failures_by_address.blocked(address)
    ):
        raise LoginThrottled
    resolved = await _check_credentials(ident, password)
    if resolved is None:
        _failures_by_identifier.record(ident)
        if address:
            _failures_by_address.record(address)
    return resolved


async def _check_credentials(ident: str, password: str) -> Identity | None:
    ch = get_client()
    # Email first (one email may own several namespaces → mint with all of them).
    rows = await ch.query_all(
        """
        SELECT namespace, owner_email, org, password_hash, contact_email
        FROM loom.namespaces FINAL
        WHERE contact_email = {ident:String} AND active = 1 AND password_hash != ''
        """,
        params={"ident": ident},
    )
    if not rows:  # fallback: identifier is a namespace name
        rows = await ch.query_all(
            """
            SELECT namespace, owner_email, org, password_hash, contact_email
            FROM loom.namespaces FINAL
            WHERE namespace = {ident:String} AND active = 1 AND password_hash != ''
            """,
            params={"ident": ident},
        )
    if not rows:
        await verify_password_off_loop(await asyncio.to_thread(_unknown_account_hash), password)
        return None
    # The password proves one credential, not every row that shares the
    # email or name: sign in as the row it verifies, and mint only the
    # namespaces behind that same stored hash in that same org. Another
    # password row for the address (a sign-up race, another org) stays out.
    primary = None
    for r in rows:
        if await verify_password_off_loop(r["password_hash"], password):
            primary = r
            break
    if primary is None:
        return None
    same_credential = [
        r
        for r in rows
        if r is not primary
        and r["org"] == primary["org"]
        and r["password_hash"] == primary["password_hash"]
    ]
    return await _with_restricted_grants(_identity_from_rows([primary, *same_credential]))


async def _with_restricted_grants(
    identity: Identity,
) -> Identity:
    """Add the restricted namespaces the person holds a grant on to a resolved
    identity (password and OIDC share it so the two never drift on the
    namespace set)."""
    subject, org, namespaces, roles = identity
    namespaces = await _without_cut_namespaces(subject, org, namespaces)
    return subject, org, await widen_with_restricted_grants(subject, org, namespaces), roles


async def _without_cut_namespaces(subject: str, org: str, namespaces: list[str]) -> list[str]:
    """The owned `namespaces` minus those a grant of the person's was taken
    away on: owning the registry row is not access once a co-admin revoked or
    lowered the grant. When the cutoffs cannot be read, raise
    `SignInUnavailable` (503): failing open would hand a cut namespace back,
    and failing narrow would mint a token without the person's namespaces."""
    from server.auth.aliases import TooManySpellings
    from server.auth.revocation import read_grant_cutoffs

    try:
        ended = await read_grant_cutoffs(org, subject, namespaces, issued_at=None)
    except TooManySpellings:
        raise  # permanent: an operator's fix, not a retry
    except Exception as e:
        log.warning("login: grant cutoff read failed for %r in %r: %s", subject, org, e)
        raise SignInUnavailable from e
    return [ns for ns in namespaces if ns not in ended]


async def widen_with_restricted_grants(subject: str, org: str, namespaces: list[str]) -> list[str]:
    """`namespaces` (the person's own, first — their order is the read
    priority) followed by `restricted_grant_namespaces`, deduplicated. Only
    restricted rows are added — see the module docstring. A failed read
    leaves the set as it was: a login must never fail on this lookup, and the
    owned namespaces alone are the safe fallback."""
    try:
        restricted = await restricted_grant_namespaces(subject, org, strict=False)
    except Exception:
        log.warning(
            "login: restricted-grant lookup failed for %r in %r; token names owned namespaces only",
            subject,
            org,
            exc_info=True,
        )
        return list(namespaces)
    own = set(namespaces)
    return list(namespaces) + [ns for ns in restricted if ns not in own]


async def restricted_grant_namespaces(subject: str, org: str, *, strict: bool) -> list[str]:
    """Every restricted namespace in `org` the subject holds an active,
    unrevoked direct grant on, sorted. The grants read raises when it fails;
    the alias read before it does too with `strict`, and otherwise falls back
    to the subject alone."""
    from server.auth.aliases import subject_aliases
    from server.core.grant_sql import LIVE_GRANT

    ch = get_client()
    subjects = [s.lower() for s in await subject_aliases(org, subject, ch=ch, strict=strict)]
    # LIVE_GRANT's columns are the grant row's: the namespaces side selects
    # none of them.
    rows = await ch.query_all(
        """
        SELECT g.namespace AS namespace
        FROM (SELECT * FROM loom.namespace_grants FINAL) AS g
        INNER JOIN (SELECT org, namespace, classification, active
                    FROM loom.namespaces FINAL) AS n
            ON n.org = g.org AND n.namespace = g.namespace
        WHERE g.org = {org:String}
          AND lower(g.subject) IN {subs:Array(String)}
          AND {live}
          AND toString(n.classification) = 'restricted'
          AND ifNull(n.active, true) = true
        ORDER BY g.namespace
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "subs": subjects},
    )
    return sorted({str(r.get("namespace") or "") for r in rows if r.get("namespace")})


def _identity_from_rows(rows: list[dict[str, Any]], *, verified_email: str = "") -> Identity:
    """Map matched loom.namespaces rows → (subject, org, namespaces, roles).

    Shared by password login and OIDC so the two can't drift on identity /
    namespace-set / admin-role resolution. `verified_email` is the address an
    identity provider vouched for; password login has none.
    """
    primary = rows[0]
    subject = primary["owner_email"]
    namespaces = sorted({r["namespace"] for r in rows})
    return (
        subject,
        primary["org"],
        namespaces,
        platform_roles(subject, verified_email=verified_email),
    )


def platform_roles(subject: str, *, verified_email: str = "") -> list[str]:
    """`["platform_admin"]` when LOOM_PLATFORM_ADMINS names this principal,
    else `["writer"]`.

    An email entry matches only an email an identity provider verified:
    self-signup never verifies `contact_email`, so matching it would let anyone
    who types an operator's address first become platform admin. A handle entry
    (no `@`) matches the login subject, which first-claim signup binds to one
    account.
    """
    from server.settings import settings

    admins = settings.platform_admins_set
    handle = subject.strip().lower()
    email = verified_email.strip().lower()
    if ("@" not in handle and handle in admins) or (email and email in admins):
        return ["platform_admin"]
    return ["writer"]


async def refreshed_roles(subject: str, org: str, roles: list[str]) -> list[str] | None:
    """The roles an OAuth refresh may carry now, or None when the subject has
    no active account left in `org` (deactivated, claim-released) — the refresh
    chain ends there. Re-derived from LOOM_PLATFORM_ADMINS, so removing a host
    demotes their next refresh; a refresh never promotes. The stored
    contact_email stands in for the verified email: platform_admin was only
    granted at sign-in when the IdP verified it (`platform_roles`)."""
    row = await get_client().query_one(
        """
        SELECT contact_email FROM loom.namespaces FINAL
        WHERE org = {org:String} AND owner_email = {sub:String} AND active = 1
        LIMIT 1
        """,
        params={"org": org, "sub": subject},
    )
    if row is None:
        return None
    if "platform_admin" not in roles:
        return ["writer"]
    return platform_roles(subject, verified_email=str(row.get("contact_email") or ""))


async def resolve_identity_for_email(
    email: str,
) -> Identity | None:
    """Map an externally-verified email (e.g. an OIDC id_token's) to a loom
    identity. No password — the IdP already authenticated the user. Matches
    active namespaces by contact_email; password_hash is NOT required, so
    SSO-only accounts (no local password) resolve. None when nothing matches.
    """
    ch = get_client()
    ident = email.strip().lower()
    rows = await ch.query_all(
        """
        SELECT namespace, owner_email, org, password_hash, contact_email
        FROM loom.namespaces FINAL
        WHERE contact_email = {ident:String} AND active = 1
        ORDER BY org, namespace
        """,
        params={"ident": ident},
    )
    if not rows:
        return None
    # Fail closed on cross-org ambiguity: an externally-verified email must map
    # to exactly one org, else we can't safely choose the caller's tenant (a
    # namespace in another org sharing this contact_email could otherwise
    # capture the login). Same email may own several namespaces in ONE org.
    if len({r["org"] for r in rows}) > 1:
        return None
    return await _with_restricted_grants(_identity_from_rows(rows, verified_email=ident))


@router.post("/v1/auth/login", response_model=LoginOutput)
async def login(input: LoginInput, request: Request) -> LoginOutput:
    # Before the grant cutoffs are read: a cutoff landing after the read postdates the token.
    issued_at = int(time.time())
    try:
        resolved = await resolve_user_credentials(
            input.identifier, input.password, address=client_address(request)
        )
    except LoginThrottled as e:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": "Too many failed sign-ins; try again later.",
            },
            headers={"Retry-After": "900"},
        ) from e
    except SignInUnavailable as e:
        raise HTTPException(
            status_code=503,
            detail={"code": "AUTH_UNAVAILABLE", "message": "Sign-in is unavailable; try again."},
            headers={"Retry-After": "5"},
        ) from e
    if resolved is None:
        raise HTTPException(status_code=401, detail=_BAD_CREDS)
    subject, org, namespaces, roles = resolved
    token = mint_token(
        subject=subject,
        org=org,
        namespaces=namespaces,
        roles=roles,
        ttl_seconds=_JWT_TTL_SECONDS,
        is_service_account=False,
        issued_at=issued_at,
    )
    expires_at = datetime.fromtimestamp(issued_at, UTC).replace(tzinfo=None) + timedelta(
        seconds=_JWT_TTL_SECONDS
    )
    return LoginOutput(
        token=token,
        subject=subject,
        org=org,
        namespaces=namespaces,
        expires_at=expires_at,
    )


def attach(app: Any) -> None:
    app.include_router(router)
