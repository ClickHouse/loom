"""Prove two explicit bot keys before an operator provisions their public purposes."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.cli import cell_each_http as transport
from server.cli import cell_each_state as private_files

if TYPE_CHECKING:
    import httpx


@dataclass(frozen=True)
class BotKey:
    name: str
    thumbprint: str
    key_file: Path


def _refuse() -> ValueError:
    return ValueError("Bot provisioning proof refused")


def _text(value: str, maximum: int) -> bool:
    if type(value) is not str:
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    return (
        bool(value)
        and size <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _inputs(cell: str, deployment_id: str, automation: BotKey, recovery: BotKey) -> None:
    if not _text(cell, 32) or re.fullmatch(r"[a-z0-9-]{1,32}", cell) is None:
        raise _refuse()
    if not _text(deployment_id, 256):
        raise _refuse()
    for bot in (automation, recovery):
        if (
            not _text(bot.name, 256)
            or type(bot.thumbprint) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", bot.thumbprint) is None
        ):
            raise _refuse()
    if automation.name == recovery.name or automation.thumbprint == recovery.thumbprint:
        raise _refuse()


def _whoami(answer: dict[str, Any], name: str) -> str:
    if answer.keys() != {
        "authenticated",
        "subject",
        "org",
        "namespaces",
        "roles",
        "is_service_account",
        "kid",
        "exp",
        "iat",
    }:
        raise _refuse()
    expires, issued = answer["exp"], answer["iat"]
    if (
        answer["authenticated"] is not True
        or answer["is_service_account"] is not True
        or answer["subject"] != "sa:" + name
        or answer["namespaces"] != ["*"]
        or answer["roles"] != ["PLATFORM_ADMIN"]
        or not _text(answer["org"], 256)
        or not _text(answer["kid"], 256)
        or type(expires) is not int
        or type(issued) is not int
        or not 0 < expires - issued <= 60
        or not 35 <= expires - time.time() <= 60
    ):
        raise _refuse()
    org: str = answer["org"]
    return org


def _registration(
    answer: dict[str, Any], cell: str, deployment_id: str, bot: BotKey, org: str
) -> None:
    if answer.keys() != {
        "version",
        "cell",
        "deployment_id",
        "name",
        "org",
        "thumbprint",
        "active",
        "platform_admin",
    } or not transport._version(answer["version"]):
        raise _refuse()
    expected = (cell, deployment_id, bot.name, org, bot.thumbprint)
    actual = tuple(answer[key] for key in ("cell", "deployment_id", "name", "org", "thumbprint"))
    if actual != expected or answer["active"] is not True or answer["platform_admin"] is not True:
        raise _refuse()


async def _one(
    client: httpx.AsyncClient,
    origin: str,
    cell: str,
    deployment_id: str,
    bot: BotKey,
    deadline: float,
) -> None:
    endpoint = origin + "/oauth/token"
    pem = private_files.read_private(bot.key_file, 65536)
    assertion = transport.bot_assertion(
        pem, name=bot.name, thumbprint=bot.thumbprint, endpoint=endpoint
    )
    issued = time.monotonic()
    status, answer = await transport._request(
        client,
        "POST",
        endpoint,
        limit=16384,
        deadline=deadline,
        form={
            "grant_type": "client_credentials",
            "client_id": bot.name,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "expires_in": "60",
        },
    )
    if status != 200:
        raise _refuse()
    token, _expires = transport.bot_access(answer, bot.name, issued)
    status, answer = await transport._request(
        client, "GET", origin + "/v1/whoami", bearer=token, limit=2048, deadline=deadline
    )
    if status != 200:
        raise _refuse()
    org = _whoami(answer, bot.name)
    status, answer = await transport._request(
        client,
        "POST",
        origin + "/v1/admin/bots.list?self_only=true",
        bearer=token,
        limit=2048,
        deadline=deadline,
    )
    if status != 200:
        raise _refuse()
    _registration(answer, cell, deployment_id, bot, org)


async def prove_pair(
    *,
    cell: str,
    deployment_id: str,
    origin: str,
    automation: BotKey,
    recovery: BotKey,
    ca_file: Path | None = None,
) -> list[dict[str, str]]:
    """Return public records after two live proofs; perform no metadata write or action."""
    import httpx

    _inputs(cell, deployment_id, automation, recovery)
    origin = transport._origin(origin)
    tls = transport._tls({"ca_file": ca_file} if ca_file is not None else {})
    deadline = time.monotonic() + 30.0
    async with asyncio.timeout_at(deadline):
        async with httpx.AsyncClient(
            verify=tls, follow_redirects=False, trust_env=False, timeout=15.0
        ) as client:
            status, metadata = await transport._request(
                client,
                "GET",
                origin + "/.well-known/oauth-authorization-server",
                limit=16384,
                deadline=deadline,
            )
            if (
                status != 200
                or metadata.get("token_endpoint") != origin + "/oauth/token"
                or (metadata.get("issuer", origin) != origin)
            ):
                raise _refuse()
            # Both purposes require their own key proof, never a fallback for an action.
            await _one(client, origin, cell, deployment_id, automation, deadline)
            await _one(client, origin, cell, deployment_id, recovery, deadline)
    return [
        {"purpose": purpose, "name": bot.name, "thumbprint": bot.thumbprint}
        for purpose, bot in (("automation", automation), ("recovery", recovery))
    ]
