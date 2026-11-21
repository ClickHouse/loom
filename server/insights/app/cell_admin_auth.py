"""Fresh positive authority for a cell's registered automation administrator."""

from __future__ import annotations

import asyncio
import json
import math
import re
import ssl
import time
from dataclasses import dataclass
from typing import Annotated, Any, cast
from urllib.parse import quote, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, HTTPException, Request

from server.auth import keyring, machines, revocation
from server.auth.jwt import AuthInfo
from server.core import cell
from server.core.ch import set_query_tag
from server.core.ch_bounded import BoundedReadError, decode_json
from server.insights.api.models_cell_admin import CellTargetContext
from server.insights.app.admin_routes import require_admin
from server.insights.store import cell_admin_auth as repository
from server.operations.base import Role
from server.settings import settings


@dataclass(frozen=True)
class AutomationBinding:
    cell: str
    deployment_id: str
    name: str
    thumbprint: str


@dataclass(frozen=True)
class VerifiedCellAdmin:
    auth: AuthInfo
    binding: AutomationBinding


@dataclass(frozen=True)
class RegisteredBot:
    auth: AuthInfo
    cell: str
    deployment_id: str
    name: str
    thumbprint: str


def _refuse(code: str, status: int = 403) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _principal(auth: AuthInfo | None) -> AuthInfo:
    if (
        not settings.auth_actually_required()
        or not isinstance(auth, AuthInfo)
        or auth.is_service_account is not True
        or not auth.has_role(Role.PLATFORM_ADMIN)
        or auth.namespaces not in ((), ("*",))
        or any(
            (
                auth.ns_roles,
                auth.write_namespaces,
                auth.impersonated_by,
                auth.read_scope,
                auth.read_user_id,
                auth.parents,
                auth.parent_subjects,
                auth.parent_iats,
                auth.creator,
                auth.session_id,
                auth.first_party,
                auth.agent_principal,
                auth.saves_to,
            )
        )
    ):
        raise _refuse("automation_required")
    if not auth.subject.startswith("sa:") or not auth.subject[3:] or not auth.token_id:
        raise _refuse("automation_required")
    return auth


async def require_cell_admin(
    request: Request,
    auth: Annotated[AuthInfo | None, Depends(require_admin)],
) -> AuthInfo:
    """Require a bot and strict JSON before a cell action can do business work."""
    principal = _principal(auth)
    # The front-door middleware caps bytes; retain raw fields for duplicate detection.
    try:
        decode_json((await request.body()).decode("utf-8"))
    except (BoundedReadError, UnicodeError):
        raise _refuse("invalid_arguments", 400) from None
    return principal


def _security_fresh(auth: AuthInfo) -> None:
    ages = (keyring.snapshot_age_seconds(), revocation.snapshot_age_seconds())
    if (
        not keyring.loaded()
        or not revocation.loaded()
        or any(
            not math.isfinite(age) or age < 0 or age > settings.jwt_revocation_max_stale_seconds
            for age in ages
        )
    ):
        raise _refuse("security_unavailable", 503)
    now = time.time()
    if (
        type(auth.issued_at) is not int
        or type(auth.expires_at) is not int
        or not 0 < auth.expires_at - auth.issued_at <= 60
        or auth.issued_at > now + 30
        or not 35 <= auth.expires_at - now <= 60
    ):
        raise _refuse("short_token_required")


def _live_cell() -> tuple[str, str]:
    if not settings.cell or not cell.rows_read() or cell.mode() != "enforced" or cell.gate_reason():
        raise _refuse("cell_unavailable", 503)
    return settings.cell, cell.deployment_id()


def _accepted(context: CellTargetContext) -> None:
    if (context.expected_cell, context.expected_deployment_id) != _live_cell():
        raise _refuse("cell_identity_changed")


def _router_origin() -> str:
    raw = settings.cell_router_url
    try:
        parsed = urlsplit(raw)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in ("", "/")
            and raw == raw.strip()
            and "\\" not in raw
            and not any(ord(char) < 33 or ord(char) == 127 for char in raw)
        )
    except ValueError:
        valid = False
    if not valid or not settings.cell_router_token:
        raise _refuse("automation_unprovisioned", 503)
    return f"https://{parsed.netloc}"


def _pair(value: Any) -> tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"name", "thumbprint"}:
        raise BoundedReadError("Invalid automation metadata")
    name, thumbprint = value["name"], value["thumbprint"]
    if (
        not isinstance(name, str)
        or not name
        or len(name.encode("utf-8")) > 256
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or not isinstance(thumbprint, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", thumbprint) is None
    ):
        raise BoundedReadError("Invalid automation metadata")
    return name, thumbprint


def _binding(payload: bytes, context: CellTargetContext) -> AutomationBinding:
    value = decode_json(payload.decode("utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "cell", "deployment_id", "automation", "recovery"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise BoundedReadError("Invalid automation metadata")
    automation, recovery = _pair(value["automation"]), _pair(value["recovery"])
    if automation[0] == recovery[0] or automation[1] == recovery[1]:
        raise BoundedReadError("Automation and recovery must be distinct")
    if (value["cell"], value["deployment_id"], automation[0]) != (
        context.expected_cell,
        context.expected_deployment_id,
        context.expected_bot,
    ):
        raise _refuse("automation_identity_changed")
    return AutomationBinding(value["cell"], value["deployment_id"], *automation)


async def _authority(context: CellTargetContext, deadline: float) -> AutomationBinding:
    origin = _router_origin()
    remaining = min(2.0, deadline - time.monotonic())
    if remaining <= 0:
        raise _refuse("security_unavailable", 503)
    headers = {
        "Authorization": f"Bearer {settings.cell_router_token}",
        "Accept-Encoding": "identity",
    }
    url = f"{origin}/cells/v1/cells/{quote(settings.cell, safe='')}/administration"
    async with asyncio.timeout(remaining):
        async with httpx.AsyncClient(
            verify=ssl.create_default_context(),
            trust_env=False,
            follow_redirects=False,
            timeout=remaining,
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                if (
                    response.status_code != 200
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                ):
                    raise _refuse("automation_unprovisioned", 503)
                data = bytearray()
                async for chunk in response.aiter_raw():
                    if len(data) + len(chunk) > 8192:
                        raise BoundedReadError("Automation metadata is oversized")
                    data.extend(chunk)
        return _binding(bytes(data), context)


def _public_thumbprint(raw: Any) -> str:
    if not isinstance(raw, str):
        raise _refuse("automation_key_changed")
    parsed = decode_json(raw)
    if not isinstance(parsed, dict) or set(parsed) != {"keys"}:
        raise _refuse("automation_key_changed")
    keys = parsed["keys"]
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], dict):
        raise _refuse("automation_key_changed")
    if set(keys[0]) - {"kty", "crv", "x", "y", "n", "e", "alg", "use", "kid"}:
        raise _refuse("automation_key_changed")
    key = jwt.PyJWKSet.from_json(raw).keys[0]
    public = key.key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    canonical = decode_json(machines.public_jwks(public))["keys"][0]
    if keys[0] != canonical:
        raise _refuse("automation_key_changed")
    return str(canonical["kid"])


def _registered(row: dict[str, Any] | None, name: str, auth: AuthInfo) -> str:
    if row is None or (
        row.get("name") != name
        or row.get("org") != auth.org
        or type(row.get("active")) is not int
        or row["active"] != 1
        or type(row.get("platform_admin")) is not int
        or row["platform_admin"] != 1
        or row.get("claims") != {}
        or any(row.get(field) != "" for field in ("issuer", "audience", "agent_claim", "jwks_uri"))
    ):
        raise _refuse("automation_required")
    maximum = row.get("max_access_token_s")
    if row.get("ns_roles") != [] or type(maximum) is not int or not 60 <= maximum <= 86400:
        raise _refuse("automation_required")
    return _public_thumbprint(row.get("public_keys"))


def _revoked(rows: list[dict[str, Any]], issued_at: int) -> bool:
    for row in rows:
        outright, cutoff = row.get("outright"), row.get("cutoff")
        if type(outright) is not int or outright not in (0, 1) or type(cutoff) is not int:
            raise BoundedReadError("Malformed revocation")
        if outright or revocation.kills(cutoff, issued_at):
            return True
    return False


async def verify_registered_bot(
    auth: AuthInfo | None, *, deadline: float, ch: Any = None
) -> RegisteredBot:
    """Prove only the caller's live sole-key registration, without assigning a purpose."""
    auth = _principal(auth)
    set_query_tag("cell.admin.security")
    _security_fresh(auth)
    identity = _live_cell()
    name = auth.subject[3:]
    try:
        row = await repository.machine(name, deadline=deadline, ch=ch)
        thumbprint = _registered(row, name, auth)
        keys = sorted(
            {
                key
                for subject in (auth.subject, auth.token_id)
                for key in (
                    subject,
                    revocation.CUTOFF_PREFIX + subject,
                    revocation.SCIM_PREFIX + subject,
                )
            }
        )
        rows = await repository.revocations(keys, deadline=deadline, ch=ch)
        if _revoked(rows, cast(int, auth.issued_at)):
            raise _refuse("automation_revoked")
    except (
        BoundedReadError,
        UnicodeError,
        ValueError,
        TypeError,
        AttributeError,
        jwt.PyJWTError,
        httpx.HTTPError,
        TimeoutError,
    ):
        raise _refuse("security_unavailable", 503) from None
    _security_fresh(auth)
    if _live_cell() != identity:
        raise _refuse("cell_identity_changed")
    return RegisteredBot(auth, *identity, name, thumbprint)


async def registered_bot_identity(auth: AuthInfo | None) -> dict[str, Any]:
    """The caller's bounded public proof; it grants no automation or recovery purpose."""
    # not a setting: bot proof shares the fixed thirty-second cell action envelope
    async with asyncio.timeout(30):
        bot = await verify_registered_bot(auth, deadline=time.monotonic() + 30)
        answer = {
            "version": 1,
            "cell": bot.cell,
            "deployment_id": bot.deployment_id,
            "name": bot.name,
            "org": bot.auth.org,
            "thumbprint": bot.thumbprint,
            "active": True,
            "platform_admin": True,
        }
        if len(json.dumps(answer, allow_nan=False).encode()) > 2048:
            raise _refuse("security_unavailable", 503)
        return answer


async def verify_automation(
    auth: AuthInfo, context: CellTargetContext, *, deadline: float, ch: Any = None
) -> VerifiedCellAdmin:
    """Freshly bind this request to accepted cell metadata and its sole bot key."""
    set_query_tag("cell.admin.security")
    auth = _principal(auth)
    _security_fresh(auth)
    _accepted(context)
    try:
        binding = await _authority(context, deadline)
    except (BoundedReadError, UnicodeError, ValueError, httpx.HTTPError, TimeoutError):
        raise _refuse("security_unavailable", 503) from None
    bot = await verify_registered_bot(auth, deadline=deadline, ch=ch)
    if bot.name != binding.name or bot.thumbprint != binding.thumbprint:
        raise _refuse("automation_identity_changed")
    _accepted(context)
    return VerifiedCellAdmin(auth, binding)
