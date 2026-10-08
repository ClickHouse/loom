"""What the deployment serves and what this token may call —
`Agent.capabilities()` / `can_call()`, token-claim decoding, role ranking.
Server-free: httpx.MockTransport."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

import httpx
import loom
from loom import Agent, Capabilities, SyncAgent
from loom.token import ROLE_RANK, decode_token_claims, role_on, role_satisfies


def _jwt(payload: dict[str, Any]) -> str:
    def b64(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")

    return f"{b64(json.dumps({'alg': 'HS256', 'kid': 'k1'}))}.{b64(json.dumps(payload))}.sig"


WRITER = _jwt(
    {"sub": "sa:bot", "org": "acme", "namespaces": ["*"], "roles": ["WRITER"], "exp": 1_800_000_000}
)
SCOPED = _jwt(
    {
        "sub": "sa:bot",
        "org": "acme",
        "namespaces": ["support", "ci"],
        "roles": ["viewer"],
        "ns_roles": [["support", "admin"], ["ci", "viewer"]],
    }
)
CAPS = {
    "build_commit": "abc1234",
    "schema_version": "1",
    "ops": [
        {"name": "memory.get", "required_role": "VIEWER"},
        {"name": "memory.set", "required_role": "WRITER"},
        {"name": "conflicts.list", "required_role": "ADMIN"},
        {"name": "cost.budget_status", "required_role": "ADMIN"},
        {"name": "admin.users", "required_role": "ORG_ADMIN"},
        {"name": "ops.strange", "required_role": "SUPERUSER"},
    ],
}


def _agent(token: str | None, namespace: str = "support") -> tuple[Agent, list[str]]:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        return httpx.Response(200, json=CAPS)

    a = Agent(org="acme", namespace=namespace, agent="bot", api_key=token, base_url="http://x")
    a.transport._client = httpx.AsyncClient(
        base_url="http://x",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )
    return a, calls


def test_token_claims_decode_without_verifying_and_refuse_non_jwts() -> None:
    c = decode_token_claims(WRITER)
    assert c is not None
    assert (c.subject, c.org, c.namespaces, c.roles, c.ns_roles, c.exp) == (
        "sa:bot",
        "acme",
        ("*",),
        ("writer",),
        (),
        1_800_000_000,
    )
    s = decode_token_claims(SCOPED)
    assert s is not None and s.ns_roles == (("support", "admin"), ("ci", "viewer"))
    assert decode_token_claims("opaque-token") is None
    assert decode_token_claims("a.b.c") is None
    assert decode_token_claims(None) is None
    d = decode_token_claims(_jwt({"ns_roles": {"support": "WRITER"}}))
    assert d is not None and d.ns_roles == (("support", "writer"),)
    assert loom.decode_token_claims is decode_token_claims and loom.ROLE_RANK is ROLE_RANK


def test_both_sdks_role_ladders_are_the_servers() -> None:
    from server.operations.base import Role

    server = {r.name.lower(): r.value for r in Role}
    assert ROLE_RANK == server
    ts = (Path(__file__).resolve().parents[2] / "sdk/typescript/src/token.ts").read_text()
    body = re.search(r"ROLE_RANK[^{]*\{([^}]*)\}", ts)
    assert body is not None
    assert {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", body.group(1))} == server


def test_roles_rank_like_the_server_and_unknown_names_answer_none() -> None:
    assert role_satisfies("writer", "VIEWER") is True
    assert role_satisfies("writer", "admin") is False
    assert role_satisfies("writer", "superuser") is None
    assert role_satisfies(None, "viewer") is None
    sc = decode_token_claims(SCOPED)
    assert role_on(sc, "support") == "admin" and role_on(sc, "ci") == "viewer"
    assert role_on(sc, "elsewhere") == "admin" and role_on(sc) == "admin"
    assert role_on(decode_token_claims(WRITER), "anything") == "writer"


async def test_capabilities_answer_can_call_and_cache() -> None:
    a, calls = _agent(WRITER)
    caps = await a.capabilities()
    assert isinstance(caps, Capabilities) and caps.build_commit == "abc1234"
    assert caps.has("memory.set") and caps.required_role("conflicts.list") == "admin"
    assert caps.role() == "writer"
    assert caps.can_call("memory.get") is True and caps.can_call("memory.set") is True
    assert caps.can_call("conflicts.list") is False and caps.can_call("admin.users") is False
    assert caps.can_call("ops.strange") is None and caps.can_call("memory.nope") is None
    assert caps.callable() == ["memory.get", "memory.set"]
    assert await a.can_call("cost.budget_status") is False
    await a.capabilities()
    assert len(calls) == 1
    await a.capabilities(max_age_s=0)
    assert len(calls) == 2
    await a.close()


async def test_scoped_token_uses_the_agents_namespace_and_opaque_tokens_cannot_tell() -> None:
    ci, _ = _agent(SCOPED, namespace="ci")
    sup, _ = _agent(SCOPED, namespace="support")
    assert await ci.can_call("memory.set") is False
    assert await sup.can_call("conflicts.list") is True
    caps = await ci.capabilities()
    assert (
        caps.can_call("conflicts.list", "support") is True
        and caps.can_call("conflicts.list", "ci") is False
    )
    opaque, _ = _agent("opaque-token")
    oc = await opaque.capabilities()
    assert oc.claims is None and oc.can_call("memory.get") is None and oc.callable() == []
    for x in (ci, sup, opaque):
        await x.close()


def test_sync_agent_twins_block() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=CAPS)

    with SyncAgent(
        org="acme", namespace="support", agent="bot", api_key=WRITER, base_url="http://x"
    ) as sa:
        sa.transport._client = httpx.AsyncClient(
            base_url="http://x",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": f"Bearer {WRITER}"},
        )
        assert sa.can_call("conflicts.list") is False
        assert sa.capabilities().callable() == ["memory.get", "memory.set"]
