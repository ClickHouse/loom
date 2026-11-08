"""The assistant's LibreChat service user and agent, set up at boot.

One LibreChat user, Loom's service user, owns the agent definition, and its agents API
key runs it. The key reads nothing in Loom: each answer's tools read under that answer's
own token. A boot sets the service up holding the cluster lease `assistant.service`,
re-reads the row, and does only what is missing, storing each result before the next
step, so a boot that stopped halfway leaves the row at a step the next one finishes:

1. no row: write the email and a sealed random password;
2. no key: sign in (registering first only when LibreChat says the email does not
   exist), delete any key a cut-short boot left, create the key;
3. no agent id: keep the oldest agent of the definition's name and delete the rest, or
   create one;
4. a stored definition older than this build's, or of its version with another model:
   write it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any

import httpx
import yaml

from server.assistant.domain import definition as agent_definition
from server.assistant.store import service as store
from server.core import leases, paths
from server.core.ch import set_query_tag, statement_timeout
from server.core.ch_errors import error_line
from server.core.errors import error_kind
from server.core.secrets_at_rest import seal, unseal
from server.settings import settings

log = logging.getLogger(__name__)

_LEASE = "assistant.service"
_LEASE_TTL_S = 60  # not a setting: the statements under it are bounded by its share
_SEALED_PURPOSE = "assistant.service"
# What LibreChat answers a sign-in for an email it does not know.
_NO_SUCH_EMAIL = "Email does not exist"
# LibreChat's signed-in routes refuse a client that is not a browser (an `Illegal
# request` event, with status 200), so the setup names itself as one.
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"

# Whether this process has the service set up: None = not yet tried, False = a problem.
_ready: bool | None = None
_problem = ""
_agent_id = ""


class ServiceRefused(RuntimeError):
    """A refusal Loom wrote, safe to show as is."""


def assistant_state() -> tuple[bool | None, str]:
    """(ready, why not) for health and `loom doctor`."""
    return _ready, _problem


def configured() -> bool:
    return bool(settings.assistant_librechat_url)


def current_definition() -> agent_definition.Definition:
    """This build's definition: agent.yaml, the assistant toolset's tools, and
    LOOM_ASSISTANT_MODEL in place of the file's model when set."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import MCP_ASSISTANT_OPS

    raw = yaml.safe_load(paths.ASSISTANT_AGENT.read_text())
    tools = [
        OPERATIONS_BY_NAME[name].mcp_tool_name
        for name in MCP_ASSISTANT_OPS
        if name in OPERATIONS_BY_NAME
    ]
    return agent_definition.parse(raw, tools, str(settings.assistant_model))


@contextlib.asynccontextmanager
async def lease(ch: Any = None) -> AsyncIterator[None]:
    """Hold `assistant.service`, with every statement under it bounded inside it."""
    async with leases.cluster_lease(_LEASE, ttl_s=_LEASE_TTL_S, wait_s=120, ch=ch):
        with statement_timeout(leases.statement_bound(_LEASE_TTL_S)):
            yield


def _opened(sealed: str) -> dict[str, str] | None:
    plain = unseal(_SEALED_PURPOSE, sealed)
    return json.loads(plain) if plain else None


def _sealed(password: str, api_key: str) -> str:
    return seal(_SEALED_PURPOSE, json.dumps({"password": password, "api_key": api_key}))


class _LibreChat:
    """LibreChat's REST API, signed in once, at the first step that needs it."""

    def __init__(self, client: httpx.AsyncClient, email: str, password: str) -> None:
        self._client = client
        self._email = email
        self._password = password
        self._token = ""

    async def _auth(self) -> dict[str, str]:
        if not self._token:
            self._token = await self._sign_in()
        return {"Authorization": f"Bearer {self._token}"}

    async def _sign_in(self) -> str:
        body = {"email": self._email, "password": self._password}
        r = await self._client.post("/api/auth/login", json=body)
        if r.status_code != 200 and _NO_SUCH_EMAIL in r.text:
            # Registering only after LibreChat said the email is unknown: it logs a
            # register request, password included, when the email is already in use.
            name = self._email.split("@", 1)[0]
            r = await self._client.post(
                "/api/auth/register",
                json={
                    "name": name,
                    "username": name,
                    "email": self._email,
                    "password": self._password,
                    "confirm_password": self._password,
                },
            )
            if r.status_code != 200:
                raise ServiceRefused(
                    f"LibreChat refused to register the service user ({r.status_code})"
                )
            r = await self._client.post("/api/auth/login", json=body)
        if r.status_code == 429:
            raise ServiceRefused(
                "LibreChat limited the service user's sign-ins (429); the next try waits "
                "for its window"
            )
        if r.status_code != 200:
            raise ServiceRefused(
                f"LibreChat refused the service user's sign-in ({r.status_code}): empty "
                "LibreChat's database and drop loom.assistant_service; the next boot "
                "recreates both"
            )
        return str(r.json()["token"])

    async def replace_key(self, name: str) -> str:
        """A new agents API key named `name`, after deleting any of that name."""
        auth = await self._auth()
        r = await self._client.get("/api/api-keys", headers=auth)
        r.raise_for_status()
        for key in r.json().get("keys", []):
            if key.get("name") == name:
                await self._client.delete(f"/api/api-keys/{key['id']}", headers=auth)
        r = await self._client.post("/api/api-keys", headers=auth, json={"name": name})
        r.raise_for_status()
        return str(r.json()["key"])

    async def one_agent(self, defn: agent_definition.Definition) -> str:
        """The id of the one agent named as the definition: the oldest kept, the rest
        deleted, or a new one."""
        auth = await self._auth()
        r = await self._client.get("/api/agents", headers=auth, params={"limit": 100})
        r.raise_for_status()
        mine = sorted(
            (a for a in r.json().get("data", []) if a.get("name") == defn.name),
            key=lambda a: str(a.get("createdAt", "")),
        )
        for extra in mine[1:]:
            await self._client.delete(f"/api/agents/{extra['id']}", headers=auth)
        if mine:
            return str(mine[0]["id"])
        r = await self._client.post("/api/agents", headers=auth, json=defn.agent_body())
        r.raise_for_status()
        return str(r.json()["id"])

    async def write_agent(self, agent_id: str, defn: agent_definition.Definition) -> None:
        auth = await self._auth()
        r = await self._client.patch(
            f"/api/agents/{agent_id}", headers=auth, json=defn.agent_body()
        )
        r.raise_for_status()


async def ensure_service() -> bool:
    """Set the service user and agent up, doing only what the row lacks. True once
    the row has a key, an agent id and this build's definition (or a newer one)."""
    global _agent_id
    defn = current_definition()
    async with lease():
        row = await store.read()
        if row is None:
            row = store.ServiceRow(
                email=str(settings.assistant_service_email),
                sealed=_sealed(secrets.token_urlsafe(24), ""),
            ).advanced()
            await store.write(row)
        opened = _opened(row.sealed)
        if opened is None:
            raise ServiceRefused(
                "loom.assistant_service does not open with LOOM_JWT_SECRET (set "
                "LOOM_JWT_SECRET_PREVIOUS to the secret it was sealed under and run "
                "`loom secrets reseal`)"
            )
        async with httpx.AsyncClient(
            base_url=str(settings.assistant_librechat_url),
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,  # not a setting: each step is one small request, under the lease
        ) as client:
            lc = _LibreChat(client, row.email, opened["password"])
            if not opened.get("api_key"):
                key = await lc.replace_key(defn.name)
                row = row.advanced(sealed=_sealed(opened["password"], key))
                await store.write(row)
            if not row.agent_id:
                row = row.advanced(agent_id=await lc.one_agent(defn))
                await store.write(row)
            if agent_definition.needs_write(row.definition_version, row.definition_model, defn):
                await lc.write_agent(row.agent_id, defn)
                row = row.advanced(definition_version=defn.version, definition_model=defn.model)
                await store.write(row)
    _agent_id = row.agent_id
    return True


async def _setup_once() -> bool:
    global _ready, _problem
    try:
        await ensure_service()
    except Exception as e:  # noqa: BLE001 — a failed setup is a reported problem, never fatal to boot
        _record_failure(e)
        return False
    _ready, _problem = True, ""
    return True


def _record_failure(e: BaseException) -> None:
    """A refusal Loom wrote is shown as it is; anything else, whose text can carry a
    URL or LibreChat's answer, as its kind, with the text in the log."""
    global _ready, _problem
    refused = isinstance(e, ServiceRefused)
    problem = (
        f"setting up the assistant in LibreChat failed: {e.args[0] if refused else error_kind(e)}"
    )
    if problem != _problem:
        log.warning("%s", problem if refused else f"{problem} ({error_line(e)})")
    _ready, _problem = False, problem


def start_setup_loop() -> asyncio.Task[None] | None:
    """Set the service up in this process's background, again every interval until a
    run succeeds; None when no LibreChat is configured."""
    if not configured():
        return None

    async def _run() -> None:
        set_query_tag("assistant.service")  # every process runs it, not a worker
        while True:
            if await _setup_once():
                return
            await asyncio.sleep(float(settings.assistant_setup_retry_s))

    return asyncio.get_running_loop().create_task(_run(), name="loom-assistant-service")


__all__ = [
    "assistant_state",
    "configured",
    "current_definition",
    "ensure_service",
    "start_setup_loop",
]
