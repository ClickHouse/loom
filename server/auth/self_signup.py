"""Browser-driven namespace self-signup endpoint.

Gated by `LOOM_SELF_SIGNUP=open`. When enabled, any caller reaching
`POST /v1/auth/self-signup` can mint a JWT scoped to a namespace they
choose. The token carries:
    sub        = the namespace name (caller's identity going forward)
    org        = settings.self_signup_org (a request naming another is refused)
    namespaces = [<the namespace>]
    roles      = ['writer'], or ['platform_admin'] for a handle in
                 LOOM_PLATFORM_ADMINS — never for the unverified email
    sa         = false  (user token, not service account)

For trusted-network deployments where reachability is the access
boundary. Hardened deployments should front this with a federated
upstream (oauth2-proxy, Cloudflare Access, custom) that mints
short-lived Loom JWTs from verified identity claims.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.auth.attempts import Attempts, client_address
from server.auth.jwt import mint_token
from server.auth.login import platform_roles
from server.auth.passwords import hash_password_off_loop
from server.core.ch import get_client
from server.core.datetimes import naive_utc_now_seconds
from server.core.naming import SSO_NAMESPACE_PREFIX
from server.settings import settings

router = APIRouter()


async def provision_account(
    *,
    org: str,
    namespace: str,
    subject: str,
    contact_email: str,
    password_hash: str = "",
    description: str = "",
    tags: list[str] | None = None,
    ch: Any = None,
) -> None:
    """Create a loom.namespaces row + the owner's admin self-grant.

    Shared by self-signup (password), OIDC JIT provisioning (no password),
    agent self-signup (the claim code as the password), the auth-off default
    namespace and the personal-namespace auto-claim, so the account shape
    can't drift. password_hash="" = no password sign-in. `ch` defaults to
    the server's client.
    """
    ch = ch or get_client()
    now = naive_utc_now_seconds()
    await ch.insert(
        "loom.namespaces",
        [
            {
                "org": org,
                "namespace": namespace,
                "owner_email": subject,
                "contact_email": contact_email,
                "password_hash": password_hash,
                "description": description,
                "tags": list(tags or []),
                "active": True,
                "conflict_threshold": None,
                "retention_days": 0,
                "warm_days": 0,
                "visibility": "private",
                "classification": "standard",
                "join_policy": "inherit",
                "created_at": now,
                "updated_at": now,
            }
        ],
    )
    await ch.insert(
        "loom.namespace_grants",
        [
            {
                "org": org,
                "namespace": namespace,
                "subject": subject,
                "role": "admin",
                "granted_at": now,
                "granted_by": subject,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


class SelfSignupInput(BaseModel):
    namespace: Annotated[str, Field(min_length=1, max_length=128)]
    org: str | None = None  # defaults to settings.self_signup_org
    email: Annotated[str, Field(min_length=3, max_length=320)]
    # argon2id-hashed before storage; repr=False keeps it out of logs and tracebacks
    password: Annotated[str, Field(min_length=8, max_length=256, repr=False)]
    ttl_days: Annotated[int, Field(ge=1, le=3650)] = 365


class SelfSignupOutput(BaseModel):
    token: str = Field(repr=False)
    namespace: str
    org: str
    subject: str
    namespaces: list[str] = []
    expires_at: datetime | None = None


def _signup_gate() -> None:
    """The two doors self-signup checks before doing anything (shared by the
    human and the agent routes)."""
    # One way in: when the deployment is configured for SSO, identity comes from
    # the IdP (JIT-provisioned on first login). Leaving password self-signup open
    # would be an SSO bypass — so the single oidc toggle closes it.
    if settings.oauth_login_provider == "oidc":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SELF_SIGNUP_SSO",
                "message": "this deployment uses SSO; sign in via your identity provider",
            },
        )
    if settings.self_signup != "open":
        raise HTTPException(
            status_code=404,
            detail={
                "code": "SELF_SIGNUP_DISABLED",
                "message": (
                    "self-signup is off; ask the operator to set "
                    "LOOM_SELF_SIGNUP=open or to mint you a token via the CLI"
                ),
            },
        )


@router.post("/v1/auth/self-signup", response_model=SelfSignupOutput)
async def self_signup(input: SelfSignupInput, request: Request) -> SelfSignupOutput:
    _signup_gate()
    org = signup_org(input.org)
    namespace = _claimable_namespace(input.namespace)
    if not _human_signups.allow(client_address(request)):
        raise _rate_limited(f"at most {HUMAN_SIGNUPS_PER_HOUR} sign-ups per address per hour")
    # Name lock first, then the email's: one fixed order, so two claims never
    # wait on each other's second lock. The email lock makes EMAIL_IN_USE's
    # check-then-insert exclusive within this process too.
    email_key = "email:" + (input.email or "").strip().lower()
    async with _claim_lock(org, namespace), _claim_lock(org, email_key):
        return await _claim_account(input, org, namespace)


async def _claim_account(input: SelfSignupInput, org: str, namespace: str) -> SelfSignupOutput:
    subject = namespace

    # First-claim-wins. If the namespace already exists, refuse — without
    # this, a second caller typing the same name receives a JWT with the
    # same `sub`, effectively impersonating the original claimer. The
    # operator can release a stuck claim via `loom claim-release`.
    ch = get_client()
    existing = await ch.query_one(
        """
        SELECT toString(ifNull(owner_email, '')) AS owner
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NAMESPACE_CLAIMED",
                "message": (
                    f"namespace {namespace!r} is already claimed. "
                    "Pick a different name; if you're the original claimer "
                    "and lost your token, ask the operator to run "
                    "`loom claim-release` for this namespace."
                ),
            },
        )

    contact_email = (input.email or "").strip().lower()
    if "@" not in contact_email or "." not in contact_email.split("@", 1)[1]:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_EMAIL",
                "message": "email must be a valid address",
            },
        )
    # One email = one human. Sharing contact_email across namespaces lets
    # login pick one arbitrarily — an account-takeover surface.
    existing = await ch.query_one(
        """
        SELECT count() AS n
        FROM loom.namespaces FINAL
        WHERE org = {org:String}
          AND lower(contact_email) = {ce:String}
          AND active = 1
          AND password_hash != ''
        """,
        params={"org": org, "ce": contact_email},
    )
    if existing and int(existing.get("n") or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "EMAIL_IN_USE",
                "message": (
                    f"An account already exists for {contact_email}. "
                    "Sign in with that email instead, or create the new "
                    "namespace from your existing account."
                ),
            },
        )
    ttl_seconds = int(input.ttl_days) * 86400
    # The email is unverified, so it never confers a role (platform_roles).
    token = mint_token(
        subject=subject,
        org=org,
        namespaces=[namespace],
        roles=platform_roles(subject),
        ttl_seconds=ttl_seconds,
        is_service_account=False,
    )
    await provision_account(
        org=org,
        namespace=namespace,
        subject=subject,
        contact_email=contact_email,
        password_hash=await hash_password_off_loop(input.password),
        description="Created via self-signup.",
    )

    # Approximate expiry; mint_token uses time.time() internally.
    expires_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(
        seconds=ttl_seconds
    )

    return SelfSignupOutput(
        token=token,
        namespace=namespace,
        namespaces=[namespace],
        org=org,
        subject=subject,
        expires_at=expires_at,
    )


def signup_org(requested: str | None) -> str:
    """The org every signed-up account lands in: `self_signup_org`. A caller
    naming another org is refused — letting it pick would open a tenant per
    caller and dodge the per-org EMAIL_IN_USE and first-claim checks."""
    org = settings.self_signup_org
    if requested and requested != org:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SIGNUP_ORG_FIXED",
                "message": f"self-signup creates accounts in org {org!r} only",
            },
        )
    return org


def _claimable_namespace(name: str) -> str:
    """The namespace name, which is also the account's token subject, in the
    shared namespace shape — so it can never spell an email, a service-account
    `sa:` subject or a wildcard that grants under that subject would honour.
    `personal-*` and the SSO prefix are reserved for auto-provisioning."""
    from server.core.naming import validate_namespace_name

    try:
        namespace = validate_namespace_name(name)
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail={"code": "VALIDATION_FAILED", "message": str(e)}
        ) from e
    for prefix in ("personal-", SSO_NAMESPACE_PREFIX):
        if namespace.startswith(prefix):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "VALIDATION_FAILED",
                    "message": f"'{prefix}' names are reserved for auto-provisioned namespaces",
                },
            )
    return namespace


# ============================================================
# Agent self-signup: a bot mints its own scoped token; a
# human claims the namespace later with a one-time code.
# ============================================================

AGENT_NAMESPACE_PREFIX = "agent-"
AGENT_SIGNUPS_PER_HOUR = 5
# Humans behind one office NAT share an address; agents rarely do.
HUMAN_SIGNUPS_PER_HOUR = 20
_AGENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,55}$")
# Sign-ups per address per hour; the trust boundary is reachability, and a
# hardened deployment fronts this with a gateway, as the module docstring says.
_agent_signups = Attempts(AGENT_SIGNUPS_PER_HOUR, 3600)
_human_signups = Attempts(HUMAN_SIGNUPS_PER_HOUR, 3600)
_MAX_TRACKED = 10_000
# Known limit: per-process locks per claimed name and per signup email — close
# the first-claim and EMAIL_IN_USE check-then-insert races inside one server
# process; replicas or `--workers N` still race (ClickHouse has no
# insert-if-absent), so a shared claim store (a KeeperMap key under
# keeper_map_strict_mode, as the migration lock uses) is the upgrade. Password
# sign-in picks the row the password verifies, so a raced duplicate email
# cannot sign one account into the other.
_claim_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def agent_namespace(name: str) -> str | None:
    """`agent-<slug>` for a display name, or None when nothing usable is left
    after slugging (a name of punctuation)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:56].strip("-")
    if not slug or not _AGENT_SLUG_RE.match(slug):
        return None
    return f"{AGENT_NAMESPACE_PREFIX}{slug}"


def _claim_lock(org: str, namespace: str) -> asyncio.Lock:
    """The lock a claim of (org, namespace) holds from its existence check to
    its insert. Idle locks are dropped once many names are tracked; a held or
    awaited lock is never replaced, or a later caller would skip the queue."""
    if len(_claim_locks) >= _MAX_TRACKED:
        for key in [k for k, lock in _claim_locks.items() if not lock.locked()]:
            del _claim_locks[key]
    return _claim_locks[(org, namespace)]


def _rate_limited(message: str) -> HTTPException:
    return HTTPException(status_code=429, detail={"code": "RATE_LIMITED", "message": message})


class AgentSignupInput(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=64)]
    org: str | None = None  # defaults to settings.self_signup_org
    purpose: Annotated[str, Field(max_length=500)] = ""
    ttl_days: Annotated[int, Field(ge=1, le=3650)] | None = 365


class AgentSignupOutput(BaseModel):
    token: str = Field(repr=False)
    namespace: str
    org: str
    subject: str
    claim_code: str = Field(repr=False)
    claim_url: str
    expires_at: datetime | None = None


def _base_url(request: Request) -> str:
    # One resolver for every address the server hands out.
    from server.auth.public_url import public_base_url

    return public_base_url(request)


@router.post("/v1/auth/agent-signup", response_model=AgentSignupOutput)
async def agent_signup(input: AgentSignupInput, request: Request) -> AgentSignupOutput:
    """A bot signs itself up: one call, no human, no operator.

    Provisions `agent-<slug>` with the human self-signup's account shape —
    the namespace's owner identity is the namespace name, its password is a
    one-time claim code — grants a service-account subject `writer` on it,
    and returns that subject's token plus the claim code. A person claims the
    namespace later by signing in at /me with the namespace name and the
    code (the ordinary login route), which makes them its admin; the agent's
    token keeps working. First-claim-wins on the name, like the human route.
    """
    if settings.effective_identity_provider() == "single-user":
        # Creates nothing, so the per-address limit does not apply.
        return await _single_user_agent(input, request)
    _signup_gate()
    if not _agent_signups.allow(client_address(request)):
        raise _rate_limited(f"at most {AGENT_SIGNUPS_PER_HOUR} agent sign-ups per address per hour")
    namespace = agent_namespace(input.name)
    if namespace is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "name must contain a letter or digit; it becomes agent-<slug>",
            },
        )
    org = signup_org(input.org)
    async with _claim_lock(org, namespace):
        return await _provision_agent(input, request, org, namespace)


async def _single_user_agent(input: AgentSignupInput, request: Request) -> AgentSignupOutput:
    """Single-user mode has one identity and nothing to claim: hand the agent a
    writer token for the default namespace, so it keeps working if auth is
    turned on later. Reaching this server already makes the caller its admin."""
    from server.auth.default_namespace import _SINGLE_USER_SUBJECT

    org, namespace = settings.self_signup_org, settings.default_namespace
    ttl_seconds = int(input.ttl_days) * 86400 if input.ttl_days is not None else None
    token = mint_token(
        subject=_SINGLE_USER_SUBJECT,
        org=org,
        namespaces=[namespace],
        roles=["writer"],
        ttl_seconds=ttl_seconds,
        is_service_account=False,
    )
    return AgentSignupOutput(
        token=token,
        namespace=namespace,
        org=org,
        subject=_SINGLE_USER_SUBJECT,
        claim_code="",
        claim_url=f"{_base_url(request)}/me",
        expires_at=naive_utc_now_seconds() + timedelta(seconds=ttl_seconds)
        if ttl_seconds is not None
        else None,
    )


async def _provision_agent(
    input: AgentSignupInput, request: Request, org: str, namespace: str
) -> AgentSignupOutput:
    ch = get_client()
    existing = await ch.query_one(
        """
        SELECT 1 AS x
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "NAMESPACE_CLAIMED",
                "message": (
                    f"namespace {namespace!r} already exists. Pick a different name; "
                    "if it is yours and the token is lost, ask the operator to run "
                    "`loom claim-release` for it."
                ),
            },
        )

    claim_code = secrets.token_urlsafe(18)
    sa_hash = hashlib.sha256(f"agent:{org}:{namespace}:{time.time_ns()}".encode()).hexdigest()[:6]
    sa_subject = f"sa:{namespace}-{sa_hash}"
    ttl_seconds = int(input.ttl_days) * 86400 if input.ttl_days is not None else None
    now = naive_utc_now_seconds()
    expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None

    token = mint_token(
        subject=sa_subject,
        org=org,
        namespaces=[namespace],
        roles=["writer"],
        ttl_seconds=ttl_seconds,
        is_service_account=True,
    )
    await provision_account(
        org=org,
        namespace=namespace,
        subject=namespace,
        contact_email="",
        password_hash=await hash_password_off_loop(claim_code),
        description=input.purpose.strip() or "Created via agent self-signup; unclaimed.",
        tags=["agent"],
    )
    await ch.insert(
        "loom.namespace_grants",
        [
            {
                "org": org,
                "namespace": namespace,
                "subject": sa_subject,
                "role": "writer",
                "granted_at": now,
                "granted_by": namespace,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    await ch.insert(
        "loom.service_accounts",
        [
            {
                "org": org,
                "name": f"agent · {input.name.strip()}",
                "sa_subject": sa_subject,
                "namespaces": [namespace],
                "roles": ["writer"],
                "ttl_days": int(input.ttl_days or 0),
                "created_by": namespace,
                "created_at": now,
                "expires_at": expires_at,
                "revoked_at": None,
                "revoked_by": "",
                "version": time.time_ns(),
                "kind": "agent",
                "client": "agent-signup",
                "inherit_creator_grants": False,
                "ns_roles": json.dumps({namespace: "writer"}),
            }
        ],
    )
    return AgentSignupOutput(
        token=token,
        namespace=namespace,
        org=org,
        subject=sa_subject,
        claim_code=claim_code,
        claim_url=f"{_base_url(request)}/me",
        expires_at=expires_at,
    )


def attach(app: Any) -> None:
    app.include_router(router)
