"""An MCP session's implicit run, through the transport, on the auth-on server.

A token minted with `read_scope=own_session` acts as the user and session its
calls name. The run the transport begins for its MCP session is filed under
that user and that session (not the MCP session id), every write carries the
run's stored id, and the client's DELETE /mcp completes it.

Two tokens sending the same (client-chosen) session id hold two runs: each
one's writes carry its own, and each one's DELETE completes only its own.

A DELETE right after a call, on another connection, completes the run without
freezing a count the ledger had not caught up with: every call is there by id.

Needs a server that enforces auth (the isolation step of CI); skips otherwise,
and without LOOM_BASE_URL (tests/integration/conftest.py).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

USER, APP_SESSION = "user-s", "app-session-1"


def _mint(org: str, ns: str, **claims: Any) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=f"sa:mcp-scope-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=[ns],
        roles=["writer"],
        ttl_seconds=900,
        is_service_account=True,
        **claims,
    )


def _headers(token: str, session: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Mcp-Session-Id": session,
        "Accept": "application/json, text/event-stream",
    }


async def _memory_set(c: httpx.AsyncClient, token: str, session: str, ns: str, key: str) -> None:
    arguments = {"namespace": ns, "session_id": APP_SESSION, "memory_key": key, "value": key}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_memory_set", "arguments": arguments},
    }
    r = await c.post("/mcp", json=body, headers=_headers(token, session))
    assert r.status_code == 200 and "error" not in r.json(), r.text[:400]


async def _require_auth(c: httpx.AsyncClient) -> None:
    r = await c.post("/v1/memory.get", json={"org": "o", "namespace": "n", "memory_key": "k"})
    if r.status_code == 200:
        pytest.skip("the server does not enforce auth")


@pytest.mark.asyncio
async def test_a_session_scoped_tokens_run_is_filed_under_its_session(
    control_plane_url: str, ch_client: Any
) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        await _require_auth(c)
        org, ns = unique_org("mcp-scope"), "ns"
        token = _mint(org, ns, read_scope="own_session", read_user_id=USER)
        session = uuid.uuid4().hex
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


@pytest.mark.asyncio
async def test_two_tokens_sending_one_session_id_hold_two_runs(
    control_plane_url: str, ch_client: Any
) -> None:
    org, session = unique_org("mcp-two"), uuid.uuid4().hex
    tokens = {ns: _mint(org, ns) for ns in ("ns-a", "ns-b")}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        await _require_auth(c)
        for ns, token in tokens.items():
            for i in range(2):
                await _memory_set(c, token, session, ns, f"rv/fact/{ns}/{i}")
        r = await c.delete("/mcp", headers=_headers(tokens["ns-b"], session))
        assert r.json()["run_closed"] is True, r.text[:400]

    runs = dict(
        ch_client.query(
            "SELECT namespace, status FROM loom.agent_runs FINAL WHERE org = {org:String}",
            parameters={"org": org},
        ).result_rows
    )
    assert runs == {"ns-a": "active", "ns-b": "completed"}
    stamped = ch_client.query(
        "SELECT namespace, uniqExact(run_id) FROM loom.memory FINAL WHERE org = {org:String} "
        "GROUP BY namespace ORDER BY namespace",
        parameters={"org": org},
    ).result_rows
    assert stamped == [("ns-a", 1), ("ns-b", 1)]


@pytest.mark.asyncio
async def test_a_delete_right_after_a_call_keeps_every_call_in_the_ledger(
    control_plane_url: str, ch_client: Any
) -> None:
    org, ns, session = unique_org("mcp-fast"), "ns", uuid.uuid4().hex
    token = _mint(org, ns)
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        await _require_auth(c)
        for i in range(3):
            await _memory_set(c, token, session, ns, f"rv/fact/{i}")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        r = await c.delete("/mcp", headers=_headers(token, session))
        assert r.status_code == 200 and r.json()["run_closed"] is True, r.text[:400]

    ((run_id, status, metadata),) = ch_client.query(
        "SELECT run_id, status, metadata FROM loom.agent_runs FINAL WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows
    assert status == "completed" and "calls" not in json.loads(metadata)

    async def _all_ledgered() -> bool:
        return (
            ch_client.query(
                "SELECT count() FROM loom.agent_events WHERE run_id = {rid:String} "
                "AND operation = 'memory.set'",
                parameters={"rid": run_id},
            ).result_rows[0][0]
            == 3
        )

    await eventually(_all_ledgered, description="the session's three calls in the ledger")


@pytest.mark.asyncio
async def test_a_runs_goal_never_shows_a_call_in_another_namespace(
    control_plane_url: str, ch_client: Any
) -> None:
    """The run row sits in the caller's default namespace, which other principals
    read; a first call in another namespace leaves its text out of the goal."""
    from server.auth.jwt import mint_token

    org, session = unique_org("mcp-goal"), uuid.uuid4().hex
    token = mint_token(
        subject=f"sa:mcp-goal-{uuid.uuid4().hex[:6]}",
        org=org,
        namespaces=["ns-a", "ns-b"],
        roles=["writer"],
        ttl_seconds=900,
        is_service_account=True,
    )
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        await _require_auth(c)
        await _memory_set(c, token, session, "ns-b", "SECRET-ns-b-only")
        r = await c.delete("/mcp", headers=_headers(token, session))
        assert r.json()["run_closed"] is True, r.text[:400]

    rows = ch_client.query(
        "SELECT namespace, goal FROM loom.agent_runs FINAL WHERE org = {org:String}",
        parameters={"org": org},
    ).result_rows
    assert rows and all("SECRET" not in goal for _, goal in rows)
    assert {namespace for namespace, _ in rows} == {"ns-a"}
