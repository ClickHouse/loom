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

from server.auth.attempts import Attempts, client_address
from server.auth.jwt import mint_token
from server.auth.passwords import hash_password, verify_password_off_loop
from server.core.ch import get_client
from server.core.naming import SSO_NAMESPACE_PREFIX

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
_BAD_CREDS = {"code": "AUTH_INVALID", "message": "Invalid email or password."}

# Failed sign-ins per identifier (guessing one account's password) and per
# client address (spraying many accounts), per 15 minutes.
_failures_by_identifier = Attempts(10, 900)
_failures_by_address = Attempts(50, 900)


class LoginThrottled(Exception):
    """Too many failed sign-ins for this identifier or address; retry later."""


@functools.cache
def _unknown_account_hash() -> str:
    """A real argon2id hash of a random secret, verified against when no
    account matches, so an unknown identifier costs one full hash like a
    wrong password does. A malformed placeholder would be rejected in
    microseconds, which tells an attacker which accounts exist."""
    return hash_password(secrets.token_urlsafe(24))


async def resolve_user_credentials(
    identifier: str, password: str, *, address: str = ""
) -> tuple[str, str, list[str], list[str]] | None:
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


async def _check_credentials(
    ident: str, password: str
) -> tuple[str, str, list[str], list[str]] | None:
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
    identity: tuple[str, str, list[str], list[str]],
) -> tuple[str, str, list[str], list[str]]:
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


def _identity_from_rows(
    rows: list[dict[str, Any]], *, verified_email: str = ""
) -> tuple[str, str, list[str], list[str]]:
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
) -> tuple[str, str, list[str], list[str]] | None:
    """Map an externally-verified email (e.g. an OIDC id_token's) to a loom
    identity. No password — the IdP already authenticated the user. Matches
    active SSO-only accounts (no password) by contact_email. An account made
    with a password is never matched: self-signup takes any typed email
    unverified, so matching it would hand a stranger's namespace to whoever
    owns the address at the IdP. None when nothing matches.
    """
    ch = get_client()
    ident = email.strip().lower()
    rows = await ch.query_all(
        """
        SELECT namespace, owner_email, org, password_hash, contact_email
        FROM loom.namespaces FINAL
        WHERE contact_email = {ident:String} AND active = 1 AND password_hash = ''
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


async def jit_provision_for_email(
    email: str,
) -> tuple[str, str, list[str], list[str]] | None:
    """Just-in-time provisioning for OIDC: when enabled, a verified email with
    no loom account gets a personal namespace auto-created (in oidc_jit_org),
    then resolves like any other account. Returns None when JIT is off or the
    email's domain isn't allowed (caller then rejects the login).
    """
    import hashlib

    from server.auth.self_signup import provision_account
    from server.observability import audit
    from server.settings import settings as _settings

    if not _settings.oidc_jit_provision:
        return None
    ident = email.strip().lower()
    if "@" not in ident:
        return None
    domain = ident.rsplit("@", 1)[1]
    allowed = _settings.oidc_jit_email_domains_set
    if allowed and domain not in allowed:
        return None
    # Deterministic, unique-per-email namespace: a readable slug + an email
    # hash so two different emails can never collide onto one account.
    local = ident.split("@", 1)[0]
    slug = "".join(c if c.isalnum() else "-" for c in local).strip("-")[:40] or "user"
    ns = f"{SSO_NAMESPACE_PREFIX}{slug}-{hashlib.sha256(ident.encode()).hexdigest()[:8]}"
    taken = await get_client().query_one(
        "SELECT 1 FROM loom.namespaces FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} LIMIT 1",
        params={"org": _settings.oidc_jit_org, "ns": ns},
    )
    if taken is not None:
        # Not this person's (their account would have resolved): someone made
        # it first, and their grants on it must not reach this person's data.
        log.warning("oidc jit: namespace %r already exists; refusing the sign-in", ns)
        return None
    await provision_account(
        org=_settings.oidc_jit_org,
        namespace=ns,
        subject=ns,
        contact_email=ident,
        password_hash="",  # SSO-only account
        description="Auto-provisioned via OIDC SSO.",
    )
    await audit.emit(
        "auth.jit_provisioned",
        org=_settings.oidc_jit_org,
        namespace=ns,
        actor=ident,
        subject={"provider": "oidc", "namespace": ns},
    )
    # Re-resolve through the normal mapping (applies admin-promotion + the
    # cross-org guard). A simultaneous first login for the same email
    # writes the same (org, namespace) row — ReplacingMergeTree collapses it.
    return await resolve_identity_for_email(ident)


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
                "message": "Too many failed sign-ins; try again later.",
            },
            headers={"Retry-After": "900"},
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
