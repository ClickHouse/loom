"""The platform-admin API for accounts and bots: what `loom sso` and `loom bot`
do on the host, for an admin signed in from anywhere (`loom call`, the SDK)."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.auth import machines
from server.auth.jwt import AuthInfo
from server.identity.app import account_admin
from server.insights.app.admin_routes import require_admin
from server.settings import settings

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(require_admin)])
# The verified admin (None with auth off); FastAPI runs the gate once per request.
Admin = Annotated[AuthInfo | None, Depends(require_admin)]


def _by(info: AuthInfo | None) -> str:
    return f"admin:{info.subject}" if info else "admin:local"


def _invalid(e: ValueError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": "VALIDATION_FAILED", "message": str(e)})


class AdoptInput(BaseModel):
    domain: str
    org: str = ""
    apply: bool = False
    keep_tokens: bool = False


@router.post("/v1/admin/sso.adopt")
async def sso_adopt(input: AdoptInput, info: Admin) -> dict[str, Any]:
    """The password accounts at a domain and why any is skipped; with `apply`,
    links the rest (see `loom sso adopt`)."""
    plan = await account_admin.adopt_plan(input.domain, input.org)
    linked = (
        await account_admin.adopt(plan, by=_by(info), keep_tokens=input.keep_tokens)
        if input.apply
        else 0
    )
    return {
        "accounts": plan,
        "applied": input.apply,
        "linked": linked,
        # Also their service-account and MCP tokens: the CI that uses one breaks.
        "revokes_tokens": not input.keep_tokens,
    }


class EmailInput(BaseModel):
    email: str
    org: str = ""


@router.post("/v1/admin/sso.unlink")
async def sso_unlink(input: EmailInput, info: Admin) -> dict[str, Any]:
    accounts = await account_admin.unlink(input.email, by=_by(info))
    return {"unlinked": [{"org": org, "namespace": ns} for org, ns in accounts]}


@router.post("/v1/admin/sso.password_link")
async def sso_password_link(input: EmailInput, info: Admin) -> dict[str, Any]:
    """A one-time link (a day) that gives the email's SSO accounts a password."""
    try:
        code, namespaces = await account_admin.password_link(input.email, input.org, by=_by(info))
    except ValueError as e:
        raise _invalid(e) from None
    base = (settings.oauth_issuer or settings.public_base_url).rstrip("/")
    return {"url": f"{base}/oauth/set-password?code={code}", "namespaces": namespaces}


class BotInput(BaseModel):
    name: str
    org: str
    namespace_roles: list[str]
    public_key_pem: str = ""
    generate_key: bool = False
    issuer: str = ""
    audience: str = ""
    claims: list[str] = []
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
    except Exception as e:  # noqa: BLE001 — it may be saved: its only key must not be lost
        log.error("bots.create %s may be half done: %s", input.name, e)
        answer["warning"] = (
            "the bot may be saved, its old tokens not yet revoked: send this again "
            "with replace, or use the key it carries"
        )
    return answer


@router.post("/v1/admin/bots.list")
async def bots_list() -> dict[str, Any]:
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
                "updated_by": r["updated_by"],
            }
            for r in rows
        ]
    }


class NameInput(BaseModel):
    name: str


@router.post("/v1/admin/bots.disable")
async def bots_disable(input: NameInput, info: Admin) -> dict[str, Any]:
    return {"disabled": await account_admin.disable_bot(input.name, by=_by(info))}
