"""The platform-admin API for accounts and bots: what `loom sso` and `loom bot`
do on the host, for an admin signed in from anywhere (`loom call`, the SDK)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from server.auth import agents, machines, oauth, revocation, subject_revocation
from server.auth.grant_claims import NotClaimed
from server.auth.jwt import AuthInfo
from server.core.errors import GrantBusyError
from server.core.principals import org_of_end_user_revocation
from server.identity.app import account_admin
from server.insights.app.admin_routes import require_admin

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
# The verified admin (None with auth off); FastAPI runs the gate once per request.
Admin = Annotated[AuthInfo | None, Depends(require_admin)]


def _by(info: AuthInfo | None) -> str:
    return f"admin:{info.subject}" if info else "admin:local"


def _only_a_person(info: AuthInfo | None, done: str, on_host: str) -> None:
    """Only a human admin may create lasting credentials or change human sign-in."""
    if info is None:
        return
    third_party_session = bool(info.session_id) and not info.first_party
    if info.is_service_account or info.subject.startswith("sa:") or third_party_session:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "AUTH_INSUFFICIENT",
                "message": f"{done} by a person signed in to Loom as a platform admin, or on "
                f"the host (`{on_host}`), never with a service account's token or a "
                "third-party client's session",
            },
        )


def _invalid(e: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": str(e)})


class _Input(BaseModel):
    # A misspelled field is refused, not dropped: a lost `skip` adopts the bot
    # it meant to leave out, a lost `org` unlinks an email in every org.
    model_config = ConfigDict(extra="forbid")


class AdoptInput(_Input):
    domain: str
    org: str = ""
    skip: list[str] = []  # emails left out, e.g. bots'
    apply: bool = False
    keep_tokens: bool = False


@router.post("/v1/admin/sso.adopt")
async def sso_adopt(input: AdoptInput, info: Admin) -> dict[str, Any]:
    """The password accounts at a domain and why any is skipped; with `apply`,
    links the rest (see `loom sso adopt`)."""
    if input.apply:
        _only_a_person(info, "accounts are adopted", "loom sso adopt")
    try:
        plan = await account_admin.adopt_plan(input.domain, input.org, input.skip)
        linked = (
            await account_admin.adopt(plan, by=_by(info), keep_tokens=input.keep_tokens)
            if input.apply
            else 0
        )
    except ValueError as e:
        raise _invalid(e) from None
    return {
        "accounts": plan,
        "applied": input.apply,
        "linked": linked,
        # Also their service-account and MCP tokens: the CI that uses one breaks.
        "revokes_tokens": not input.keep_tokens,
    }


class EmailInput(_Input):
    email: str
    org: str = ""


class LinkInput(_Input):
    email: str
    org: str
    namespace: str


@router.post("/v1/admin/sso.link")
async def sso_link(input: LinkInput, info: Admin) -> dict[str, Any]:
    """Point one password account at the address its owner signs in with (see `loom sso link`)."""
    _only_a_person(info, "an account is linked to an SSO address", "loom sso link")
    try:
        was, carried, elsewhere = await account_admin.link_account(
            input.email, input.org, input.namespace, by=_by(info)
        )
    except ValueError as e:
        raise _invalid(e) from None
    return {
        "linked": {"org": input.org, "namespace": input.namespace},
        "was": was,
        "carried_revoked": carried,
        # `was` is another account's verified email: its grants stay that
        # account's, and `carried_revoked` names the ones capped for this one.
        "elsewhere": elsewhere,
    }


@router.post("/v1/admin/sso.unlink")
async def sso_unlink(input: EmailInput, info: Admin) -> dict[str, Any]:
    """Detach the email's accounts from their SSO sign-in (see `loom sso unlink`)."""
    _only_a_person(info, "an SSO link is undone", "loom sso unlink")
    accounts = await account_admin.unlink(input.email, input.org, by=_by(info))
    return {"unlinked": [{"org": org, "namespace": ns} for org, ns in accounts]}


@router.post("/v1/admin/sso.password_link")
async def sso_password_link(input: EmailInput, info: Admin, request: Request) -> dict[str, Any]:
    """A one-time link (valid for `password_link_ttl_seconds`) that gives the email's SSO
    accounts a password."""
    _only_a_person(info, "an SSO account is given a password", "loom sso password-link")
    try:
        code, namespaces = await account_admin.password_link(input.email, input.org, by=_by(info))
    except ValueError as e:
        raise _invalid(e) from None
    url = f"{oauth.issuer(request)}/oauth/set-password?code={code}"
    return {"url": url, "namespaces": namespaces}


class BotInput(_Input):
    name: str
    org: str
    namespace_roles: list[str]
    public_key_pem: str = ""
    generate_key: bool = False
    issuer: str = ""
    audience: str = ""
    claims: list[str] = []
    agent_claim: str = ""
    jwks_uri: str = ""
    max_token_s: int = 3600
    max_agents: int = 1000
    max_access_token_s: int = machines.TOKEN_TTL
    # Its tokens carry platform_admin, which spans every org.
    platform_admin: bool = False
    replace: bool = False


@router.post("/v1/admin/bots.create")
async def bots_create(input: BotInput, info: Admin) -> dict[str, Any]:
    """Register a bot. With `generate_key` the answer carries its private key,
    once: Loom keeps only the public half."""
    if input.platform_admin:
        # A token that carries platform_admin must not mint itself a lasting
        # successor: a leaked token would become a permanent one.
        _only_a_person(info, "a platform-admin bot is registered", "loom bot create")
    if input.generate_key and input.public_key_pem:
        raise _invalid(ValueError("give a public key or ask for one to be generated, not both"))
    private, public = machines.generate_key() if input.generate_key else (b"", b"")
    try:
        bot = account_admin.bot_from(
            name=input.name,
            org=input.org,
            namespace_roles=input.namespace_roles,
            public_key_pem=public.decode() or input.public_key_pem,
            issuer=input.issuer,
            audience=input.audience,
            claims=input.claims,
            agent_claim=input.agent_claim,
            jwks_uri=input.jwks_uri,
            max_token_s=input.max_token_s,
            max_agents=input.max_agents,
            max_access_token_s=input.max_access_token_s,
            platform_admin=input.platform_admin,
        )
    except ValueError as e:
        raise _invalid(e) from None
    answer: dict[str, Any] = {"client_id": input.name, "subject": machines.subject(input.name)}
    if private:
        answer["private_key_pem"] = private.decode()
    try:
        await account_admin.create_bot(bot, by=_by(info), replace=input.replace)
    except ValueError as e:
        raise _invalid(e) from None
    except (GrantBusyError, NotClaimed):
        raise  # its claim was not had: nothing is saved, so the key is worth nothing
    except Exception:  # it may be saved: its only key must not be lost
        if not private:
            raise
        log.exception("bots.create %s may be half done", input.name)
        answer["warning"] = (
            "the bot may be saved, its old tokens not yet revoked: send this again "
            "with replace, or use the key it carries"
        )
    return answer


@router.post("/v1/admin/bots.list")
async def bots_list() -> dict[str, Any]:
    """Every registered bot: its namespaces and roles, key or issuer, and limits."""
    rows = await machines.listed()
    return {
        "bots": [
            {
                "name": r["name"],
                "org": r["org"],
                "active": bool(r["active"]),
                "namespace_roles": [f"{ns}={role}" for ns, role in r["ns_roles"]],
                "key": bool(r["has_key"]),
                "issuer": r["issuer"],
                "audience": r["audience"],
                "claims": dict(r["claims"]),
                "agent_claim": r["agent_claim"],
                "jwks_uri": r["jwks_uri"],
                "max_token_s": int(r["max_token_s"]),
                "max_agents": int(r["max_agents"]),
                "max_access_token_s": int(r["max_access_token_s"]),
                "platform_admin": bool(r.get("platform_admin", 0)),
                "updated_by": r["updated_by"],
            }
            for r in rows
        ]
    }


class NameInput(_Input):
    name: str


@router.post("/v1/admin/bots.disable")
async def bots_disable(input: NameInput, info: Admin) -> dict[str, Any]:
    """Disable a bot: its proofs are refused and the tokens it holds revoked."""
    return {"disabled": await account_admin.disable_bot(input.name, by=_by(info))}


class ProvisionAgentInput(_Input):
    name: str
    agent: str


@router.post("/v1/admin/bots.provision_agent")
async def bots_provision_agent(input: ProvisionAgentInput, info: Admin) -> dict[str, Any]:
    """Make the namespace of a trust's agent before its first exchange, as that
    exchange would, so grants can name the agent first.

    `name` is a bot registered with an agent claim, `agent` the value its
    workloads' claim will carry. The namespace counts against the trust's
    `max_agents`; a repeat changes nothing.
    """
    try:
        org, namespace = await account_admin.provision_agent(input.name, input.agent, by=_by(info))
    except ValueError as e:
        raise _invalid(e) from None
    return {"org": org, "namespace": namespace, "subject": agents.subject(input.name, input.agent)}


class RevokeSubjectInput(_Input):
    org: str
    subject: str
    reason: str = ""
    # Only the tokens issued before this instant (RFC 3339, with its offset); omitted: all.
    issued_before: datetime | None = None


@router.post("/v1/admin/revoke_subject")
async def revoke_subject(input: RevokeSubjectInput, info: Admin) -> dict[str, Any]:
    """Revoke every token and sign-in session of a subject, in every org and for
    good, and list the tokens it made revoked (see `loom revoke-token --subject`).

    Outright by default; with `issued_before`, only those issued before it on any
    replica (to its next second, plus the clock skew replicas may have; an
    instant ahead of the server counts as now), so a later sign-in works. A
    revocation already there is never weakened: a cutoff is recorded beside the
    subject's own revocation and only ever moves later. The spellings `org`'s
    directory pairs with the subject, its email verified or not, are revoked with
    it: a person's email and the login handle of each account naming it. `org` is
    also where the audit rows land; an end user's subject, `<org>/user:<id>`, must
    name it. `sessions` and `tokens` count the sign-in sessions and the tokens made
    by those subjects that the revocation ends, so a repeat answers the same.
    """
    org, subject = input.org, input.subject.strip()
    if not org.strip() or "*" in org or org != org.strip():
        raise _invalid(ValueError(f"org must name one org, got {org!r}"))
    problem = subject_revocation.refusal(subject)
    if input.issued_before is not None and input.issued_before.tzinfo is None:
        problem = "issued_before needs its UTC offset (RFC 3339), e.g. 2026-01-31T12:00:00Z"
    end_user_org = org_of_end_user_revocation(subject)
    if problem is None and end_user_org and end_user_org != org:
        problem = f"an end user's revocation is their org's: send org {end_user_org!r}"
    if problem:
        raise _invalid(ValueError(problem))
    # Taken once: an instant ahead of the server is clamped to its clock.
    at = None if input.issued_before is None else subject_revocation.cutoff(input.issued_before)
    done = await subject_revocation.revoke_in_org(
        org, subject, by=_by(info), reason=input.reason, at=at
    )
    return {
        "subjects": [s for s, _ in done],
        "org": org,
        "revoked_at": max(d.row["revoked_at"] for _, d in done),
        "reason": input.reason,
        # The cutoff in force for the subjects not revoked outright, the earliest
        # when theirs differ: the one asked for, or a later one set before.
        "issued_before": None if at is None else _earliest(d.cutoff for _, d in done),
        # The subjects revoked outright now: all of them after an outright
        # request, and after a cutoff those an outright revocation still holds.
        "outright": [s for s, d in done if d.outright],
        "sessions": sum(d.sessions for _, d in done),
        "tokens": sum(d.tokens for _, d in done),
    }


def _earliest(cutoffs: Iterable[datetime | None]) -> datetime | None:
    """The earliest of the cutoffs in force; None when every subject is revoked outright."""
    return min((c for c in cutoffs if c is not None), default=None)


@router.get("/v1/admin/revocations")
async def revocations(
    subject: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> dict[str, Any]:
    """The unexpired revocations, newest first, at most `limit` (1000 by default);
    `?subject=` narrows them to one subject's own row, an operator cutoff's and
    SCIM's, `?since=` to those written since (see `loom revoke-token --list`)."""
    if since is not None and since.tzinfo is None:
        raise _invalid(ValueError("since needs its UTC offset (RFC 3339)"))
    narrowed = None if subject is None else subject.strip()
    rows = await revocation.list_active(narrowed, since=since, limit=limit)
    return {"revocations": rows}
