"""Password-based sign-in: verify email-or-namespace + password, mint a JWT
with the user's full namespace set.

The namespace set is the namespaces the person owns (by
contact email) plus every *restricted* namespace they hold an active direct
grant on. A restricted namespace is read only by a token
that names it, and the live grant is re-checked on every call, so naming
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
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.auth import attempts
from server.auth.attempts import Attempts, address_key, client_address
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


# A signed-in person: (subject, org, namespaces, roles).
Identity = tuple[str, str, list[str], list[str]]

# Generic auth-failure message — never leak whether the account exists.
_BAD_CREDS = {"code": "AUTH_INVALID", "message": "Invalid email or password."}

# Failed sign-ins per identifier (guessing one account's password) and per
# client address (spraying many accounts), per 15 minutes. Each attempt is
# recorded and recounted before its password is checked, so a concurrent burst
# reaches the check at most `limit` times.
_by_identifier = Attempts("login.identifier", 10, 900)
_by_address = Attempts("login.address", 50, 900)


class LoginThrottled(Exception):
    """Too many failed sign-ins for this identifier or address; retry later."""

    retry_after_s = 900
    message = "Too many failed sign-ins; try again later."


class LoginBusy(LoginThrottled):
    """This process is checking as many sign-ins as it takes at once; retry shortly."""

    retry_after_s = 1
    message = "Too many sign-ins at once; try again in a second."


class SignInUnavailable(Exception):
    """A sign-in could not read or write what it needs; retry later."""


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
    by_address = [(_by_address, address_key(address))] if address else []
    gates = [(_by_identifier, ident), *by_address]
    try:
        async with attempts.slot():
            admitted = await attempts.admit(gates)
            if admitted is None:
                raise LoginThrottled
            try:
                resolved = await _check_credentials(ident, password)
            except BaseException:
                # A check that crashed (a ClickHouse timeout) judged nothing.
                await _release_quietly(admitted)
                raise
            if resolved is not None:
                await attempts.release(admitted)
            return resolved
    except attempts.Busy as e:
        raise LoginBusy from e


async def _release_quietly(admitted: attempts.Admission) -> None:
    try:
        await attempts.release(admitted)
    except Exception as e:  # noqa: BLE001 — the check's own error is the one to raise
        log.warning("login: could not release a crashed attempt: %s", e)


async def _check_credentials(ident: str, password: str) -> Identity | None:
    if not ident:
        # Unclaimed agents' rows name no email: a blank identifier would test
        # the password against every one of them. Timed as an unknown account.
        await verify_password_off_loop(await asyncio.to_thread(_unknown_account_hash), password)
        return None
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
    return subject, org, await widen_with_restricted_grants(subject, org, namespaces), roles


async def widen_with_restricted_grants(subject: str, org: str, namespaces: list[str]) -> list[str]:
    """`namespaces` (the person's own, first — their order is
    the read priority) followed by every restricted namespace in `org` the
    subject holds an active, unrevoked direct grant on, sorted, deduplicated.
    Only restricted rows are added — see the module docstring. A failed read
    leaves the set as it was: a login must never fail on this lookup, and
    the owned namespaces alone are the safe fallback."""
    ch = get_client()
    try:
        from server.auth.aliases import subject_aliases
        from server.core.grant_sql import LIVE_GRANT

        subjects = [s.lower() for s in await subject_aliases(org, subject, ch=ch)]
        # LIVE_GRANT's columns are the grant row's: the namespaces side
        # selects none of them.
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
    except Exception:
        log.warning(
            "login: restricted-grant lookup failed for %r in %r; token names owned namespaces only",
            subject,
            org,
            exc_info=True,
        )
        return list(namespaces)
    own = list(namespaces)
    seen = set(own)
    extra = sorted({str(r.get("namespace") or "") for r in rows if r.get("namespace")} - seen)
    return own + extra


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
        ORDER BY contact_email DESC  -- the row naming one, not a personal namespace's
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
    active SSO-only accounts (no password) by contact_email, and password
    accounts linked to the email (`sso_links`). An unlinked password account
    is never matched: self-signup takes any typed email unverified, so
    matching it would hand a stranger's namespace to whoever owns the address
    at the IdP. None when nothing matches.
    """
    ident = email.strip().lower()
    if not ident:  # rows with no email (agents', bots') are nobody's to sign in as
        return None
    ch = get_client()
    rows = await ch.query_all(
        """
        SELECT namespace, owner_email, org, password_hash, contact_email
        FROM loom.namespaces FINAL
        WHERE contact_email = {ident:String} AND active = 1
          AND (password_hash = '' OR (org, namespace) IN (
              SELECT org, namespace FROM loom.sso_links FINAL
              WHERE email = {ident:String} AND linked = 1
          ))
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
    # Likewise two owners (a password account linked beside another's): the
    # first row's subject would win, and with it the other's namespaces.
    if len({r["owner_email"] for r in rows}) > 1:
        return None
    return await _with_restricted_grants(_identity_from_rows(rows, verified_email=ident))


@router.post("/v1/auth/login", response_model=LoginOutput)
async def login(input: LoginInput, request: Request) -> LoginOutput:
    from server.settings import settings

    # With SSO on, the identity provider is the only way in: a local password
    # would skip its MFA and outlive a deactivation there. An operator who is
    # locked out mints a token on the host (`loom mint-token`).
    if settings.oauth_login_provider == "oidc":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "PASSWORD_LOGIN_SSO",
                "message": "this deployment uses SSO; sign in via your identity provider",
            },
        )
    try:
        resolved = await resolve_user_credentials(
            input.identifier, input.password, address=client_address(request)
        )
    except LoginThrottled as e:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "RATE_LIMITED",
                "message": e.message,
            },
            headers={"Retry-After": str(e.retry_after_s)},
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
    )
    expires_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(
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
