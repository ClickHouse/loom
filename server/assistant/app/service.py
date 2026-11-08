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

# The service user's LibreChat name: LibreChat wants at least three characters.
_USER_NAME = "loom-assistant"

# Whether this process has the service set up: None = not yet tried, False = a problem.
_ready: bool | None = None
_problem = ""
# The agent id and agents API key answers run with, once a setup succeeded.
_answering: tuple[str, str] | None = None
# This process's setup loop, while it runs.
_loop: asyncio.Task[None] | None = None


class ServiceRefused(RuntimeError):
    """A refusal Loom wrote, safe to show as is."""


def assistant_state() -> tuple[bool | None, str]:
    """(ready, why not) for `/v1/health`."""
    return _ready, _problem


def answering() -> tuple[str, str] | None:
    """(agent id, agents API key) once this process set the service up, else None."""
    return _answering


def forget_answering(why: str) -> None:
    """LibreChat no longer takes the stored key or agent (another replica replaced it,
    or LibreChat's database was): drop them and set the service up again, which reads
    the row another replica may have written or repairs it."""
    global _answering, _ready, _problem
    _answering = None
    _ready, _problem = False, f"setting up the assistant again: {why}"
    if _loop is None or _loop.done():
        start_setup_loop()


def current_definition() -> agent_definition.Definition:
    """This build's definition: agent.yaml, the tools the assistant connection lists,
    and LOOM_ASSISTANT_MODEL in place of the file's model when set."""
    from server.operations.toolsets import assistant_ops

    raw = yaml.safe_load(paths.ASSISTANT_AGENT.read_text())
    tools = [op.mcp_tool_name for op in assistant_ops()]
    return agent_definition.parse(raw, tools, str(settings.assistant_model))


@contextlib.asynccontextmanager
async def lease(ch: Any = None) -> AsyncIterator[None]:
    """Hold `assistant.service`, with every statement under it bounded inside it."""
    async with leases.cluster_lease(_LEASE, ttl_s=_LEASE_TTL_S, wait_s=120, ch=ch):
        with statement_timeout(leases.statement_bound(_LEASE_TTL_S)):
            yield


def opened(sealed: str) -> dict[str, str] | None:
    """The service user's password and API key from the row's sealed value, None when it
    does not open with the deployment's secret."""
    plain = unseal(_SEALED_PURPOSE, sealed)
    return json.loads(plain) if plain else None


def _sealed(password: str, api_key: str) -> str:
    return seal(_SEALED_PURPOSE, json.dumps({"password": password, "api_key": api_key}))


class _LibreChat:
    """LibreChat's REST API, signed in at the first step that needs it (twice when it
    registers the service user)."""

    def __init__(self, client: httpx.AsyncClient, email: str, password: str) -> None:
        self._client = client
        self._email = email
        self._password = password
        self._token = ""
        # This try registered the service user: LibreChat's database was new to it.
        self.registered = False

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
            r = await self._client.post(
                "/api/auth/register",
                json={
                    "name": _USER_NAME,
                    "username": _USER_NAME,
                    "email": self._email,
                    "password": self._password,
                    "confirm_password": self._password,
                },
            )
            if r.status_code != 200:
                raise ServiceRefused(
                    f"LibreChat refused to register the service user ({r.status_code})"
                )
            self.registered = True
            r = await self._client.post("/api/auth/login", json=body)
        if r.status_code == 429:
            raise ServiceRefused(
                "LibreChat limited the service user's sign-ins (429); the next try waits "
                "for its window"
            )
        if r.status_code >= 500:
            raise ServiceRefused(
                f"LibreChat could not sign the service user in ({r.status_code}): it or its "
                "database is unavailable; the next try asks again"
            )
        if r.status_code != 200:
            raise ServiceRefused(
                f"LibreChat refused the service user's sign-in ({r.status_code}): empty "
                "LibreChat's database and run `TRUNCATE TABLE loom.assistant_service`; "
                "the next boot recreates both"
            )
        return str(r.json()["token"])

    async def agent_status(self, api_key: str, agent_id: str) -> int:
        """What LibreChat answers the stored key for the stored agent: 200 when both
        stand, 401 when the key is gone, 404 when the agent is. No sign-in."""
        r = await self._client.get(
            f"/api/agents/v1/models/{agent_id}", headers={"Authorization": f"Bearer {api_key}"}
        )
        return r.status_code

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


async def ensure_service() -> None:
    """Set the service user and agent up, doing only what the row lacks, and check
    with the stored key that LibreChat still holds them (its database replaced, or
    the agent deleted, makes the step again)."""
    global _answering
    defn = current_definition()
    async with lease():
        row = await store.read()
        if row is None:
            row = store.ServiceRow(
                email=str(settings.assistant_service_email),
                sealed=_sealed(secrets.token_urlsafe(24), ""),
            ).advanced()
            await store.write(row)
        credentials = opened(row.sealed)
        if credentials is None:
            raise ServiceRefused(
                "loom.assistant_service does not open with LOOM_JWT_SECRET (set "
                "LOOM_JWT_SECRET_PREVIOUS to the secret it was sealed under and run "
                "`loom secrets reseal`)"
            )
        password, key = credentials["password"], credentials.get("api_key", "")
        agent_id = row.agent_id
        async with httpx.AsyncClient(
            base_url=str(settings.assistant_librechat_url),
            headers={"User-Agent": _BROWSER_UA},
            timeout=30,  # not a setting: each step is one small request, under the lease
        ) as client:
            lc = _LibreChat(client, row.email, password)
            if key and agent_id:
                key, agent_id = await _still_held(lc, key, agent_id)
            made_key = not key
            if made_key:
                key = await lc.replace_key(defn.name)
                row = row.advanced(sealed=_sealed(password, key))
                await store.write(row)
                if agent_id:
                    # The key was gone, so the user may be new and own no agent.
                    _, agent_id = await _still_held(lc, key, agent_id)
            if not agent_id:
                # A found or new agent gets this build's definition in the next step.
                row = row.advanced(
                    agent_id=await lc.one_agent(defn), definition_version=0, definition_model=""
                )
                await store.write(row)
            if lc.registered and not made_key:
                # A user this try registered cannot own the key the row held.
                key = await lc.replace_key(defn.name)
                row = row.advanced(sealed=_sealed(password, key))
                await store.write(row)
            if agent_definition.needs_write(row.definition_version, row.definition_model, defn):
                await lc.write_agent(row.agent_id, defn)
                row = row.advanced(definition_version=defn.version, definition_model=defn.model)
                await store.write(row)
    _answering = (row.agent_id, key)


async def _still_held(lc: _LibreChat, key: str, agent_id: str) -> tuple[str, str]:
    """(key, agent id), each '' when LibreChat no longer holds it."""
    status = await lc.agent_status(key, agent_id)
    if status == 401:
        return "", agent_id
    if status == 404:
        return key, ""
    if status != 200:
        raise ServiceRefused(f"LibreChat answered {status} for the assistant's agent")
    return key, agent_id


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
    """Set the service up in this process's background until a try succeeds, waiting
    LOOM_ASSISTANT_SETUP_RETRY_S after the first failure and twice as long after each
    next, up to 16 times as long; None when no LibreChat is configured. Every process of
    a replica shares one address in LibreChat's sign-in limit (seven in five minutes),
    so the waits grow."""
    if not settings.assistant_librechat_url:
        return None

    async def _run() -> None:
        set_query_tag("assistant.service")  # every process runs it, not a worker
        failures = 0
        while not await _setup_once():
            await asyncio.sleep(float(settings.assistant_setup_retry_s) * 2 ** min(failures, 4))
            failures += 1

    global _loop
    _loop = asyncio.get_running_loop().create_task(_run(), name="loom-assistant-service")
    return _loop


__all__ = [
    "answering",
    "forget_answering",
    "assistant_state",
    "current_definition",
    "ensure_service",
    "opened",
    "start_setup_loop",
]
