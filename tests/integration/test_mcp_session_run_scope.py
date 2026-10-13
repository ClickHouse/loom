"""An MCP session's implicit run, through the transport, for a session-scoped token.

A token minted with `read_scope=own_session` acts as the user and session its
calls name. The run the transport begins for its MCP session is filed under
that user and that session (not the MCP session id), every write carries the
run's stored id, and the client's DELETE /mcp completes it.

Needs a server that enforces auth (the isolation step of CI); skips otherwise,
and without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

USER, APP_SESSION = "user-s", "app-session-1"


def _mint(org: str, ns: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"sa:mcp-scope-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=["writer"],
        ttl_seconds=900,
        is_service_account=True,
        read_scope="own_session",
        read_user_id=USER,
    )


def _headers(token: str, session: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Mcp-Session-Id": session,
        "Accept": "application/json, text/event-stream",
    }


async def _memory_set(c: httpx.AsyncClient, token: str, session: str, ns: str, key: str) -> None:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "loom_memory_set",
            "arguments": {
                "namespace": ns,
                "session_id": APP_SESSION,
                "memory_key": key,
                "value": f"fact {key}",
            },
        },
    }
    r = await c.post("/mcp", json=body, headers=_headers(token, session))
    assert r.status_code == 200 and "error" not in r.json(), r.text[:400]


@pytest.mark.asyncio
async def test_a_session_scoped_tokens_run_is_filed_under_its_session(
    control_plane_url: str, ch_client: Any
) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        r = await c.post("/v1/memory.get", json={"org": "o", "namespace": "n", "memory_key": "k"})
        if r.status_code == 200:
            pytest.skip("the server does not enforce auth")
        org, ns = unique_org("mcp-scope"), "ns"
        token, session = _mint(org, ns), uuid.uuid4().hex
        for i in range(3):
            await _memory_set(c, token, session, ns, f"rv/fact/{i}")
        r = await c.delete("/mcp", headers=_headers(token, session))
        assert r.status_code == 200 and r.json()["run_closed"] is True, r.text[:400]

    runs = ch_client.query(
        "SELECT run_id, user_id, session_id, status FROM loom.agent_runs FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": org, "ns": ns},
    ).result_rows
    assert len(runs) == 1
    run_id, user, row_session, status = runs[0]
    assert (user, row_session, status) == (USER, APP_SESSION, "completed")
    stamped = ch_client.query(
        "SELECT DISTINCT run_id FROM loom.memory FINAL WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows
    assert stamped == [(run_id,)]
