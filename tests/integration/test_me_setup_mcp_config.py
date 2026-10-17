"""POST /v1/me/setup/mcp_config — Setup-tab MCP config generator.

Powers the dashboard's Settings page. The caller's existing JWT determines their
identity; the endpoint mints a fresh JWT scoped to that identity and
returns a per-client MCP config block.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt as pyjwt
import pytest

from tests.integration._eventually import eventually
from tests.integration.test_mcp import _post as _mcp_post

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


async def _auth_on(control_plane_url: str) -> bool:
    """Whether the server checks bearers: a read route answers 401 without one."""
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0) as c:
        r = await c.get("/v1/me/service_accounts")
    return r.status_code == 401


async def _post(control_plane_url: str, token: str | None, body: dict) -> httpx.Response:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=10.0, headers=headers) as c:
        return await c.post("/v1/me/setup/mcp_config", json=body)


def _tool_call(tool: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def _grant_writer(ch_client: Any, org: str, namespace: str, subject: str, *, revoke: bool) -> None:
    """A person's writer grant on `namespace`, or its revocation."""
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    row = [org, namespace, subject, "writer", now, "it", now if revoke else None, time.time_ns()]
    cols = ["org", "namespace", "subject", "role", "granted_at", "granted_by", "revoked_at"]
    ch_client.insert("loom.namespace_grants", [row], column_names=[*cols, "version"])


async def _assert_unaddressed_write_lands_in(base_url: str, token: str, namespace: str) -> None:
    """An MCP memory_set naming no namespace, read back by key in `namespace`."""
    key = f"setup-default-{uuid.uuid4().hex[:8]}"
    value = {"check": key}
    set_args = {
        "agent": "setup-check",
        "memory_key": key,
        "memory_type": "semantic",
        "value": value,
    }

    async def _write() -> dict | None:
        # The creator's new grant reaches the gate within the grant cache TTL.
        r = await _mcp_post(base_url, _tool_call("loom_memory_set", set_args), token)
        result = r.json()["result"]
        return None if result["isError"] else result

    await eventually(_write, description="unaddressed memory_set succeeds")
    get_args = {"namespace": namespace, "agent": "setup-check", "memory_key": key}
    r = await _mcp_post(base_url, _tool_call("loom_memory_get", get_args), token)
    got = r.json()["result"]["structuredContent"]
    assert got["found"] is True, got
    assert got["value"] == value


@pytest.mark.asyncio
async def test_setup_mcp_config_claude_desktop(control_plane_url: str, ch_client: Any) -> None:
    from server.auth.jwt import mint_token
    from server.auth.personal_namespace import derive_personal_slug

    # A person of their own: the test grants them the namespace the page shows.
    person = f"setup-{uuid.uuid4().hex[:8]}"
    token = mint_token(
        subject=person,
        org="clickhouse",
        namespaces=[derive_personal_slug(person)],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(control_plane_url, token, {"client": "claude-desktop"})
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["client"] == "claude-desktop"
    assert body["subject"] == person
    assert body["org"] == "clickhouse"
    # The namespace the page shows is where the token's unaddressed writes land.
    _grant_writer(ch_client, "clickhouse", body["namespace"], person, revoke=False)
    try:
        await _assert_unaddressed_write_lands_in(
            control_plane_url, body["token"], body["namespace"]
        )
    finally:
        _grant_writer(ch_client, "clickhouse", body["namespace"], person, revoke=True)
    assert body["mcp_url"].endswith("/mcp")
    assert body["config_key"] == "mcpServers"
    # `namespace` in the response is the write target; reads
    # follow the creator's grants. This body named no namespace, so the
    # bearer is wildcard ("all"); the UI always names one ("single").
    assert body["namespace_scope"] == "all"
    assert body["read_scope"] == "inherited_grants"

    # Config shape — top-level mcpServers wrapper with the loom entry.
    # Claude Desktop is stdio-only, so the entry is the `npx mcp-remote`
    # bridge form (command + args carrying URL + auth header) rather
    # than the URL+headers form that claude-code/cursor/vscode use.
    cfg = body["config"]
    assert "mcpServers" in cfg
    loom = cfg["mcpServers"]["loom"]
    assert loom["command"] == "npx"
    args = loom["args"]
    assert "mcp-remote" in args
    assert body["mcp_url"] in args
    # The bearer header is passed via --header in args, not a separate
    # headers field on the entry.
    bearer_arg_idx = args.index("--header")
    assert args[bearer_arg_idx + 1].startswith("Authorization: Bearer ")

    # Path map — one per OS.
    paths = body["config_path"]
    assert "darwin" in paths and "Library/Application Support/Claude" in paths["darwin"]
    assert "linux" in paths and ".config/Claude" in paths["linux"]
    assert "win32" in paths and "APPDATA" in paths["win32"]

    # The minted token defaults to wildcard namespace so the MCP client
    # can target any workspace the caller has access to (via JWT or
    # grants) without re-issuing. Explicit `namespace` in the body
    # scopes the token to one workspace (see test below).
    decoded = pyjwt.decode(body["token"], options={"verify_signature": False})
    # Saved to alice's token list under its own subject.
    assert decoded["sub"] == body["sa_subject"] and decoded["sub"].startswith("sa:claude-desktop-")
    assert decoded["org"] == "clickhouse"
    assert decoded["namespaces"] == ["*"]
    assert body["namespace_scope"] == "all"


def _save_default(ch_client: Any, org: str, subject: str, namespace: str) -> None:
    """The person's dashboard switcher pick ("" clears it)."""
    row = [org, subject, namespace, time.time_ns()]
    ch_client.insert(
        "loom.user_state", [row], column_names=["org", "subject", "default_namespace", "version"]
    )


@pytest.mark.asyncio
async def test_setup_token_names_the_saved_default_it_writes_to(
    control_plane_url: str, ch_client: Any
) -> None:
    """A person who writes two namespaces and picked the second in the switcher:
    the page names the namespace an unaddressed MCP write from the token lands in."""
    from server.auth.jwt import mint_token
    from server.auth.personal_namespace import derive_personal_slug

    person = f"setup-{uuid.uuid4().hex[:8]}"
    personal, team = derive_personal_slug(person), f"team-{uuid.uuid4().hex[:8]}"
    for ns in (personal, team):
        _grant_writer(ch_client, "clickhouse", ns, person, revoke=False)
    _save_default(ch_client, "clickhouse", person, team)
    token = mint_token(
        subject=person,
        org="clickhouse",
        namespaces=[personal],
        roles=["WRITER"],
        is_service_account=False,
    )
    body: dict[str, Any] = {}
    try:
        r = await _post(control_plane_url, token, {"client": "cursor"})
        assert r.status_code == 200, r.text
        body = r.json()
        # Single-user mode writes its one namespace; every other mode follows the pick.
        assert body["namespace"] != personal, body["namespace"]
        if body["namespace"] != team:
            _grant_writer(ch_client, "clickhouse", body["namespace"], person, revoke=False)
        await _assert_unaddressed_write_lands_in(
            control_plane_url, body["token"], body["namespace"]
        )
    finally:
        for ns in {personal, team, body.get("namespace", team)}:
            _grant_writer(ch_client, "clickhouse", ns, person, revoke=True)
        _save_default(ch_client, "clickhouse", person, "")


@pytest.mark.asyncio
async def test_setup_mcp_config_vscode_uses_servers_key(control_plane_url: str) -> None:
    """VS Code uses `servers` instead of `mcpServers`."""
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="alice",
        org="clickhouse",
        namespaces=["personal-alice"],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(control_plane_url, token, {"client": "vscode"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config_key"] == "servers"
    assert "servers" in body["config"]
    assert body["config"]["servers"]["loom"]["type"] == "http"


@pytest.mark.asyncio
async def test_setup_mcp_config_claude_code_uses_http_type(
    control_plane_url: str,
) -> None:
    """Claude Code expects `type: http` in the mcpServers entry."""
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="alice",
        org="clickhouse",
        namespaces=["personal-alice"],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(control_plane_url, token, {"client": "claude-code"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config_key"] == "mcpServers"
    assert body["config"]["mcpServers"]["loom"]["type"] == "http"


@pytest.mark.asyncio
async def test_setup_mcp_config_explicit_namespace(control_plane_url: str) -> None:
    """A named namespace is delegated only where the caller holds a live grant: the
    caller's token claim alone is not enough (server/auth/delegation.py)."""
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="alice",
        org="clickhouse",
        namespaces=["personal-alice", "team-shared"],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(
        control_plane_url,
        token,
        {
            "client": "cursor",
            "namespace": "team-shared",
        },
    )
    assert r.status_code == 403, r.text
    assert "team-shared" in r.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_setup_mcp_config_ttl_days(control_plane_url: str) -> None:
    """Every setup token expires: ttl_days when given, else 90 days."""
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="alice",
        org="clickhouse",
        namespaces=["personal-alice"],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(
        control_plane_url,
        token,
        {
            "client": "claude-desktop",
            "ttl_days": 7,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    decoded = pyjwt.decode(body["token"], options={"verify_signature": False})
    assert "exp" in decoded
    # No ttl_days: the delegation default, 90 days.
    r2 = await _post(control_plane_url, token, {"client": "claude-desktop"})
    assert r2.status_code == 200, r2.text
    decoded2 = pyjwt.decode(r2.json()["token"], options={"verify_signature": False})
    assert decoded2["exp"] - decoded2["iat"] == 90 * 86400


@pytest.mark.asyncio
async def test_setup_mcp_config_rejects_unknown_client(control_plane_url: str) -> None:
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="alice",
        org="clickhouse",
        namespaces=["personal-alice"],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(control_plane_url, token, {"client": "emacs"})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["code"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_setup_mcp_config_requires_auth(control_plane_url: str) -> None:
    """When auth_required=True and no token is presented, return 401."""
    # Ask the server, not the process env: single-user mode skips the bearer check
    # even with LOOM_AUTH_REQUIRED=true. With auth off this POST would mint a token.
    if not await _auth_on(control_plane_url):
        pytest.skip(
            "server isn't enforcing auth; set LOOM_AUTH_REQUIRED=true AND "
            "LOOM_IDENTITY_PROVIDER=trust-network to exercise this path"
        )
    r = await _post(control_plane_url, None, {"client": "claude-desktop"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_restricted_namespaces_must_be_restricted(
    control_plane_url: str,
) -> None:
    """Naming a standard namespace is refused: it is read through membership."""
    from server.auth.jwt import mint_token

    token = mint_token(
        subject="alice",
        org="clickhouse",
        namespaces=["personal-alice"],
        roles=["WRITER"],
        is_service_account=False,
    )
    r = await _post(
        control_plane_url,
        token,
        {
            "client": "claude-code",
            "namespace": "personal-alice",
            "restricted_namespaces": ["personal-alice"],
        },
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "NOT_RESTRICTED"
