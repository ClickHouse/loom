"""Browser-driven namespace self-signup endpoint.

Gated by `LOOM_SELF_SIGNUP=open`. When enabled, any caller reaching
`POST /v1/auth/self-signup` can mint a JWT scoped to a namespace they
choose. The token carries:
    sub        = the namespace name (caller's identity going forward)
    org        = settings.self_signup_org (a request naming another is refused)
    namespaces = [<the namespace>]
    roles      = ['writer']: a name a setting or a grant empowers cannot
                 be claimed (`_refuse_empowered`)
    sa         = false  (user token, not service account)

For trusted-network deployments where reachability is the access
boundary. Hardened deployments should front this with a federated
upstream (oauth2-proxy, Cloudflare Access, custom) that mints
short-lived Loom JWTs from verified identity claims.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from server.auth.attempts import Attempts, client_address
from server.auth.claim_codes import hash_claim_code_off_loop
from server.auth.directory import directory
from server.auth.grant_claims import account_change
from server.auth.jwt import mint_token
from server.auth.passwords import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    hash_password_off_loop,
)
from server.core import grant_cache, leases
from server.core.ch import get_client, sequential_reads, set_query_tag
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import GrantBusyError
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
    agent self-signup (the claim code's hash, which only the claim takes), the auth-off default
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
    await insert_owner_grant(org, namespace, subject, ch=ch)


async def insert_owner_grant(org: str, namespace: str, subject: str, *, ch: Any = None) -> None:
    """The owner's admin self-grant a provisioned account starts with."""
    ch = ch or get_client()
    await ch.insert(
        "loom.namespace_grants",
        [
            {
                "org": org,
                "namespace": namespace,
                "subject": subject,
                "role": "admin",
                "granted_at": naive_utc_now_seconds(),
                "granted_by": subject,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    await grant_cache.changed(org)


class SelfSignupInput(BaseModel):
    namespace: Annotated[str, Field(min_length=1, max_length=128)]
    org: str | None = None  # defaults to settings.self_signup_org
    email: Annotated[str, Field(min_length=3, max_length=320)]
    # argon2id-hashed before storage; repr=False keeps it out of logs and tracebacks
    password: Annotated[
        str, Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH, repr=False)
    ]
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
    set_query_tag("auth.self_signup")  # these routes are not under the transports
    _signup_gate()
    org = signup_org(input.org)
    namespace = _claimable_namespace(input.namespace)
    if not await _human_signups.allow(client_address(request)):
        raise _rate_limited(f"at most {HUMAN_SIGNUPS_PER_HOUR} sign-ups per address per hour")
    # The name's claim, then the email's: each check-then-insert runs under its
    # deployment-wide claim, so a second sign-up on any replica is refused.
    # The account claim too, which namespace.register holds (order: `grant_claims`).
    async with (
        _claimed(name_claim(org, namespace)),
        _account_claimed(org, namespace),
        _claimed(email_claim(org, input.email)),
    ):
        return await _claim_account(input, org, namespace)


async def _claim_account(input: SelfSignupInput, org: str, namespace: str) -> SelfSignupOutput:
    subject = namespace

    # First-claim-wins. If the namespace already exists, refuse — without
    # this, a second caller typing the same name receives a JWT with the
    # same `sub`, effectively impersonating the original claimer. A name in
    # use without a registry row is refused the same way (`directory().in_use`).
    ch = get_client()
    with sequential_reads():  # an account another replica just provisioned
        return await _claim_checked(input, org, namespace, subject, ch)


async def _claim_checked(
    input: SelfSignupInput, org: str, namespace: str, subject: str, ch: Any
) -> SelfSignupOutput:
    existing = await ch.query_one(
        """
        SELECT toString(ifNull(owner_email, '')) AS owner
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace},
    )
    if existing is not None or await directory().in_use(org, namespace, ch=ch):
        raise _name_claimed(namespace)
    await _refuse_empowered(ch, org, namespace)

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
    # Sign-up confers no role: the email is unverified and a platform-admin
    # handle cannot be claimed here (`_refuse_empowered`).
    token = mint_token(
        subject=subject,
        org=org,
        namespaces=[namespace],
        roles=["writer"],
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


def _name_claimed(namespace: str) -> HTTPException:
    """409 for a name that is taken; a reserved name answers the same, so the
    refusal names no setting."""
    return HTTPException(
        status_code=409,
        detail={
            "code": "NAMESPACE_CLAIMED",
            "message": f"namespace {namespace!r} is already claimed. Pick a different name.",
        },
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
    `personal-*` is reserved for per-subject auto-provisioning and a collection name for
    `collection.create`, as `namespace.register` refuses it. (SSO's `u-`
    names need no check here: self-signup is closed whenever SSO is on.)"""
    from server.auth.sharing import is_collection
    from server.core.naming import validate_namespace_name

    try:
        namespace = validate_namespace_name(name)
    except ValueError as e:
        raise HTTPException(
            status_code=422, detail={"code": "VALIDATION_FAILED", "message": str(e)}
        ) from e
    if is_collection(namespace):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "collection names are made with collection.create",
            },
        )
    if namespace.startswith("personal-"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "'personal-' names are reserved for auto-provisioned namespaces",
            },
        )
    return namespace


async def _refuse_empowered(ch: Any, org: str, name: str) -> None:
    """Refuse, as a taken name, a name a setting empowers, or one holding an unrevoked
    grant, a namespace, an unrevoked token or a task inbox row (its owner, a poster, a
    poster rule) in the org, read case-folded as grants are;
    docs/architecture/security.md has why."""
    trusted = {s.lower() for s in settings.trusted_callers_set}
    if name in trusted or name in settings.platform_admins_set:
        raise _name_claimed(name)
    reserved = await ch.query_one(
        """
        SELECT 1 AS held
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND lower(subject) = {name:String} AND revoked_at IS NULL
        LIMIT 1
        UNION ALL
        SELECT 1 AS held
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND lower(owner_email) = {name:String}
        LIMIT 1
        UNION ALL
        SELECT 1 AS held
        FROM loom.service_accounts FINAL
        WHERE org = {org:String} AND lower(created_by) = {name:String} AND revoked_at IS NULL
        LIMIT 1
        UNION ALL
        SELECT 1 AS held
        FROM loom.task_inboxes FINAL
        WHERE org = {org:String} AND is_deleted = 0
          AND (has(arrayMap(p -> lower(p), posters), {name:String})
               OR lower(created_by) IN ('human:' || {name:String}, 'process:' || {name:String}))
        LIMIT 1
        UNION ALL
        SELECT 1 AS held
        FROM loom.task_poster_policies FINAL
        WHERE org = {org:String} AND is_deleted = 0 AND lower(poster) = {name:String}
        LIMIT 1
        """,
        params={"org": org, "name": name},
    )
    if reserved is not None:
        raise _name_claimed(name)


# ============================================================
# Agent self-signup: a bot mints its own scoped token; a
# human claims the namespace later with a code that works once.
# ============================================================

AGENT_NAMESPACE_PREFIX = "agent-"
# The description when an agent's sign-up names no purpose.
AGENT_DESCRIPTION = "Created via agent self-signup."
# The default of rows signed up before the claim page; a claim replaces it.
AGENT_DESCRIPTION_UNCLAIMED = "Created via agent self-signup; unclaimed."
# An agent's own token row (`loom.service_accounts`) is named this plus the
# agent's name; a person's token cannot take such a name.
AGENT_NAME_PREFIX = "agent · "
AGENT_CLIENT = "agent-signup"  # the `client` of that row
AGENT_SIGNUPS_PER_HOUR = 5
# Humans behind one office NAT share an address; agents rarely do.
HUMAN_SIGNUPS_PER_HOUR = 20
_AGENT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,55}$")
# Sign-ups per address per hour; the trust boundary is reachability, and a
# hardened deployment fronts this with a gateway, as the module docstring says.
_agent_signups = Attempts("signup.agent", AGENT_SIGNUPS_PER_HOUR, 3600)
_human_signups = Attempts("signup.human", HUMAN_SIGNUPS_PER_HOUR, 3600)
# A claim outlives the check, the password hash and the inserts with room to spare;
# a holder that dies is taken over after it.
CLAIM_TTL_S = 60
# The account claim's wait inside the name claim; another change answers SIGNUP_IN_PROGRESS.
ACCOUNT_WAIT_S = 2.0  # not a setting: kept well under the name claim's work share


def agent_namespace(name: str) -> str | None:
    """`agent-<slug>` for a display name, or None when nothing usable is left
    after slugging (a name of punctuation)."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:56].strip("-")
    if not slug or not _AGENT_SLUG_RE.match(slug):
        return None
    return f"{AGENT_NAMESPACE_PREFIX}{slug}"


def name_claim(org: str, namespace: str) -> str:
    return f"signup:{org}:{namespace}"


def email_claim(org: str, email: str) -> str:
    # Hashed: the claim store is no place for an address.
    digest = hashlib.sha256((email or "").strip().lower().encode()).hexdigest()[:32]
    return f"signup-email:{org}:{digest}"


def _in_progress() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "SIGNUP_IN_PROGRESS",
            "message": "another sign-up for this name or email is running; retry in a moment",
        },
    )


@asynccontextmanager
async def _claimed(name: str) -> AsyncIterator[None]:
    """Hold the deployment-wide claim `name` from the existence check to the insert;
    409 SIGNUP_IN_PROGRESS while another sign-up holds it, 503 when the work ran
    past its bound (`leases.holding`)."""
    try:
        async with leases.holding(name, ttl_s=CLAIM_TTL_S) as held:
            if not held:
                raise _in_progress()
            yield
    except leases.ClaimExpired as e:
        raise HTTPException(
            status_code=503,
            detail={"code": "SIGNUP_UNAVAILABLE", "message": "sign-up timed out; retry"},
            headers={"Retry-After": "5"},
        ) from e


@asynccontextmanager
async def _account_claimed(org: str, namespace: str) -> AsyncIterator[None]:
    """`account_change`, with a busy claim answered as another sign-up's (409
    SIGNUP_IN_PROGRESS)."""
    async with AsyncExitStack() as stack:
        try:
            # A short wait, statements bounded as the name claim's are; a slow change
            # ends in a 503 from this claim's bound or the name claim's.
            claim = account_change(
                org,
                namespace,
                wait_s=ACCOUNT_WAIT_S,
                statement_s=leases.statement_bound(CLAIM_TTL_S),
            )
            await stack.enter_async_context(claim)
        except GrantBusyError:
            raise _in_progress() from None
        yield


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


def claim_url(base_url: str, namespace: str) -> str:
    """The page where a person claims `namespace` with its code."""
    return f"{base_url}/oauth/agent-claim?{urlencode({'namespace': namespace})}"


def _base_url(request: Request) -> str:
    # One resolver for every address the server hands out.
    from server.auth.public_url import public_base_url

    return public_base_url(request)


@router.post("/v1/auth/agent-signup", response_model=AgentSignupOutput)
async def agent_signup(input: AgentSignupInput, request: Request) -> AgentSignupOutput:
    """A bot signs itself up: one call, no human, no operator.

    Provisions `agent-<slug>` with the human self-signup's account shape —
    the namespace's owner identity is the namespace name, and it stores a claim
    code that no sign-in takes — grants a service-account subject `writer` on
    it, and returns that subject's token plus the claim code. A person claims
    the namespace once at `claim_url` with the code and a password of their
    own, which replaces the code (`server.identity.app.agent_claim`); they
    then sign in as the namespace, its admin, and the agent's token keeps
    working. First-claim-wins on the name, like the human route.
    """
    set_query_tag("auth.agent_signup")
    if settings.effective_identity_provider() == "single-user":
        # Creates nothing, so the per-address limit does not apply.
        return await _single_user_agent(input, request)
    _signup_gate()
    if not await _agent_signups.allow(client_address(request)):
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
    async with _claimed(name_claim(org, namespace)), _account_claimed(org, namespace):
        with sequential_reads():
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
        claim_url=f"{_base_url(request)}/",
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
    if existing is not None or await directory().in_use(org, namespace, ch=ch):
        raise _name_claimed(namespace)
    # A person claims the account later under the name, as its subject.
    await _refuse_empowered(ch, org, namespace)

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
        password_hash=await hash_claim_code_off_loop(claim_code),
        description=input.purpose.strip() or AGENT_DESCRIPTION,
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
    await grant_cache.changed(org, [sa_subject])
    await ch.insert(
        "loom.service_accounts",
        [
            {
                "org": org,
                "name": f"{AGENT_NAME_PREFIX}{input.name.strip()}",
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
                "client": AGENT_CLIENT,
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
        claim_url=claim_url(_base_url(request), namespace),
        expires_at=expires_at,
    )


def attach(app: Any) -> None:
    app.include_router(router)
