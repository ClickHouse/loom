"""The platform-admin API for accounts and bots: what `loom sso` and `loom bot`
do on the host, for an admin signed in from anywhere (`loom call`, the SDK)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
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
    try:
        was, carried = await account_admin.link_account(
            input.email, input.org, input.namespace, by=_by(info)
        )
    except ValueError as e:
        raise _invalid(e) from None
    return {
        "linked": {"org": input.org, "namespace": input.namespace},
        "was": was,
        "carried_revoked": carried,
    }


@router.post("/v1/admin/sso.unlink")
async def sso_unlink(input: EmailInput, info: Admin) -> dict[str, Any]:
    """Detach the email's accounts from their SSO sign-in (see `loom sso unlink`)."""
    accounts = await account_admin.unlink(input.email, input.org, by=_by(info))
    return {"unlinked": [{"org": org, "namespace": ns} for org, ns in accounts]}


@router.post("/v1/admin/sso.password_link")
async def sso_password_link(input: EmailInput, info: Admin, request: Request) -> dict[str, Any]:
    """A one-time link (valid for `password_link_ttl_seconds`) that gives the email's SSO
    accounts a password."""
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
    replace: bool = False


@router.post("/v1/admin/bots.create")
async def bots_create(input: BotInput, info: Admin) -> dict[str, Any]:
    """Register a bot. With `generate_key` the answer carries its private key,
    once: Loom keeps only the public half."""
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


# How far ahead of this server's clock `issued_before` may be: a caller's clock
# running a little ahead, never a cutoff that refuses the subject's next sign-in.
_AHEAD_S = 300  # not a setting: it bounds a caller's clock error, nothing an operator tunes


def _refused_cutoff(issued_before: datetime | None) -> str | None:
    if issued_before is None:
        return None
    if issued_before.tzinfo is None:
        return "issued_before needs its UTC offset (RFC 3339), e.g. 2026-01-31T12:00:00Z"
    if issued_before > datetime.now(UTC) + timedelta(seconds=_AHEAD_S):
        return "issued_before is in the future: send the time the revocation is for"
    return None


@router.post("/v1/admin/revoke_subject")
async def revoke_subject(input: RevokeSubjectInput, info: Admin) -> dict[str, Any]:
    """Revoke every token and sign-in session of a subject, in every org and for
    good, and list the tokens it made revoked (see `loom revoke-token --subject`).

    Outright by default; with `issued_before`, only those issued before it on any
    replica (to its next second, plus the clock skew replicas may have), so a
    later sign-in works. A revocation already there is never weakened: an
    outright one, or a later cutoff, stays. The spellings `org`'s directory ties
    to the subject are revoked with it, as grants read them: a person's email and
    the login handle of the account it signs in to. `org` is also where the audit
    rows land; an end user's subject, `<org>/user:<id>`, must name it. `sessions`
    and `tokens` count the sign-in sessions and the tokens made by those subjects
    that the revocation ends, so a repeat answers the same.
    """
    org, subject = input.org, input.subject.strip()
    if not org.strip() or "*" in org or org != org.strip():
        raise _invalid(ValueError(f"org must name one org, got {org!r}"))
    problem = subject_revocation.refusal(subject) or _refused_cutoff(input.issued_before)
    end_user_org = org_of_end_user_revocation(subject)
    if problem is None and end_user_org and end_user_org != org:
        problem = f"an end user's revocation is their org's: send org {end_user_org!r}"
    if problem:
        raise _invalid(ValueError(problem))
    done = await subject_revocation.revoke_in_org(
        org, subject, by=_by(info), reason=input.reason, issued_before=input.issued_before
    )
    applied = (
        None if input.issued_before is None else subject_revocation.cutoff(input.issued_before)
    )
    return {
        "subjects": [s for s, _ in done],
        "org": org,
        "revoked_at": max(d.row["revoked_at"] for _, d in done),
        "reason": input.reason,
        # The cutoff asked for, as applied (to its next second, plus the replicas'
        # clock skew); None for an outright request. A later cutoff a subject
        # already had stays in force.
        "issued_before": applied,
        # The subjects revoked outright now: all of them for an outright request,
        # those an earlier outright revocation still holds for a cutoff.
        "outright": [s for s, d in done if d.row["issued_before"] is None],
        "sessions": sum(d.sessions for _, d in done),
        "tokens": sum(d.tokens for _, d in done),
    }


@router.get("/v1/admin/revocations")
async def revocations(subject: str | None = None) -> dict[str, Any]:
    """The unexpired revocations, newest first; `?subject=` narrows them to one
    subject's own row, an operator cutoff's and SCIM's (see `loom revoke-token --list`)."""
    return {"revocations": await revocation.list_active(subject)}
