"""POST /v1/me/service_accounts.create — the one token mint.

Powers the dashboard's Settings → Tokens, `loom token create` and the SDKs. The caller's
JWT determines their identity; the route mints a fresh JWT delegated from it and returns
it with every MCP client's setup and where its unaddressed calls save.
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
from tests.integration._resources import unique_org
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
        return await c.post("/v1/me/service_accounts.create", json=body)


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
    key = f"mint-default-{uuid.uuid4().hex[:8]}"
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


def _person_token(person: str, personal: str, org: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject=person,
        org=org,
        namespaces=[personal],
        roles=["WRITER"],
        is_service_account=False,
    )


@pytest.mark.asyncio
async def test_a_default_token_reads_everything_and_carries_every_client_setup(
    control_plane_url: str, ch_client: Any
) -> None:
    from server.auth.personal_namespace import derive_personal_slug

    # A person of their own, holding their personal namespace as sign-in leaves them.
    org = unique_org("mint")
    person = f"mint-{uuid.uuid4().hex[:8]}"
    personal = derive_personal_slug(person)
    _grant_writer(ch_client, org, personal, person, revoke=False)
    r = await _post(control_plane_url, _person_token(person, personal, org), {})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["org"] == org and body["name"].startswith("token-")
    # The namespace the answer names is where the token's unaddressed writes land:
    # the person's own with login; single-user mode names the one it serves,
    # which the test grants them too.
    shown = body["saves_to"]
    if await _auth_on(control_plane_url):
        assert shown == personal
    _grant_writer(ch_client, org, shown, person, revoke=False)
    try:
        await _assert_unaddressed_write_lands_in(control_plane_url, body["token"], shown)
    finally:
        for ns in {personal, shown}:
            _grant_writer(ch_client, org, ns, person, revoke=True)
    assert body["reads_everything"] is True and body["namespaces"] == ["*"]
    assert body["mcp_url"].endswith("/mcp")

    # Claude Desktop is stdio-only: the `npx mcp-remote` bridge, the bearer in --header.
    desktop = body["clients"]["claude-desktop"]
    assert desktop["config_key"] == "mcpServers"
    loom = desktop["config"]["mcpServers"]["loom"]
    assert loom["command"] == "npx" and "mcp-remote" in loom["args"]
    assert body["mcp_url"] in loom["args"]
    assert (
        loom["args"][loom["args"].index("--header") + 1] == f"Authorization: Bearer {body['token']}"
    )
    paths = desktop["config_path"]
    assert "Library/Application Support/Claude" in paths["darwin"]
    assert ".config/Claude" in paths["linux"] and "APPDATA" in paths["win32"]
    # VS Code uses `servers`; Claude Code an http entry on the coding toolset.
    assert body["clients"]["vscode"]["config"]["servers"]["loom"]["type"] == "http"
    code = body["clients"]["claude-code"]["config"]["mcpServers"]["loom"]
    assert code["type"] == "http" and code["url"] == f"{body['mcp_url']}?toolset=coding"

    decoded = pyjwt.decode(body["token"], options={"verify_signature": False})
    # Saved to the person's token list under its own subject.
    assert decoded["sub"] == body["sa_subject"] and decoded["sub"].startswith(f"sa:{body['name']}-")
    assert decoded["org"] == org and decoded["namespaces"] == ["*"]


def _save_default(ch_client: Any, org: str, subject: str, namespace: str) -> None:
    """The person's dashboard switcher pick ("" clears it)."""
    row = [org, subject, namespace, time.time_ns()]
    ch_client.insert(
        "loom.user_state", [row], column_names=["org", "subject", "default_namespace", "version"]
    )


@pytest.mark.asyncio
async def test_a_bare_star_token_follows_the_pick_and_a_named_home_beats_it(
    control_plane_url: str, ch_client: Any
) -> None:
    """A person who writes two namespaces and picked the second in the switcher: a bare
    `*` token saves there; one made with the first named before `*` saves to the first."""
    from server.auth.personal_namespace import derive_personal_slug

    org = unique_org("mint")
    person = f"mint-{uuid.uuid4().hex[:8]}"
    personal, team = derive_personal_slug(person), f"team-{uuid.uuid4().hex[:8]}"
    for ns in (personal, team):
        _grant_writer(ch_client, org, ns, person, revoke=False)
    _save_default(ch_client, org, person, team)
    token = _person_token(person, personal, org)
    shown: set[str] = set()
    try:
        r = await _post(control_plane_url, token, {})
        assert r.status_code == 200, r.text
        bare = r.json()
        shown.add(bare["saves_to"])
        # Single-user mode writes its one namespace; every other mode follows the pick.
        assert bare["saves_to"] != personal, bare["saves_to"]
        if bare["saves_to"] != team:
            _grant_writer(ch_client, org, bare["saves_to"], person, revoke=False)
        await _assert_unaddressed_write_lands_in(control_plane_url, bare["token"], bare["saves_to"])
        r = await _post(control_plane_url, token, {"namespaces": [personal, "*"]})
        assert r.status_code == 200, r.text
        homed = r.json()
        assert homed["saves_to"] == personal and homed["namespaces"] == [personal, "*"]
        await _assert_unaddressed_write_lands_in(control_plane_url, homed["token"], personal)
    finally:
        for ns in {personal, team, *shown}:
            _grant_writer(ch_client, org, ns, person, revoke=True)
        _save_default(ch_client, org, person, "")


@pytest.mark.asyncio
async def test_a_named_namespace_needs_a_live_grant(control_plane_url: str) -> None:
    """A named namespace is delegated only where the caller holds a live grant: the
    caller's token claim alone is not enough (server/auth/delegation.py)."""
    from server.auth.jwt import mint_token

    token = mint_token(
        subject=f"mint-{uuid.uuid4().hex[:8]}",
        org=unique_org("mint"),
        namespaces=["personal-alice", "team-shared"],
        roles=["WRITER"],
        is_service_account=False,
    )
    for namespaces in (["team-shared"], ["team-shared", "*"]):
        r = await _post(control_plane_url, token, {"namespaces": namespaces})
        assert r.status_code == 403, r.text
        assert "team-shared" in r.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_every_minted_token_expires_unless_asked_not_to(control_plane_url: str) -> None:
    """ttl_days when given, else 90 days."""
    token = _person_token(f"mint-{uuid.uuid4().hex[:8]}", "personal-alice", unique_org("mint"))
    r = await _post(control_plane_url, token, {"ttl_days": 7})
    assert r.status_code == 200, r.text
    decoded = pyjwt.decode(r.json()["token"], options={"verify_signature": False})
    assert decoded["exp"] - decoded["iat"] == 7 * 86400
    r2 = await _post(control_plane_url, token, {})
    assert r2.status_code == 200, r2.text
    decoded2 = pyjwt.decode(r2.json()["token"], options={"verify_signature": False})
    assert decoded2["exp"] - decoded2["iat"] == 90 * 86400


@pytest.mark.asyncio
async def test_a_role_beside_star_is_a_422(control_plane_url: str) -> None:
    token = _person_token(f"mint-{uuid.uuid4().hex[:8]}", "personal-alice", unique_org("mint"))
    body = {"namespaces": ["*", {"namespace": "personal-alice", "role": "viewer"}]}
    r = await _post(control_plane_url, token, body)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_the_mint_requires_auth(control_plane_url: str) -> None:
    """When auth_required=True and no token is presented, return 401."""
    # Ask the server, not the process env: single-user mode skips the bearer check
    # even with LOOM_AUTH_REQUIRED=true. With auth off this POST would mint a token.
    if not await _auth_on(control_plane_url):
        pytest.skip(
            "server isn't enforcing auth; set LOOM_AUTH_REQUIRED=true AND "
            "LOOM_IDENTITY_PROVIDER=trust-network to exercise this path"
        )
    r = await _post(control_plane_url, None, {})
    assert r.status_code == 401


def _restricted_namespace(ch_client: Any, org: str, namespace: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    row = [org, namespace, "ops@mint.invalid", True, "memory", "restricted", now, now]
    cols = ["org", "namespace", "owner_email", "active", "kind", "classification"]
    ch_client.insert("loom.namespaces", [row], column_names=[*cols, "created_at", "updated_at"])


def _grant(ch_client: Any, org: str, namespace: str, subject: str, role: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    row = [org, namespace, subject, role, now, "it", time.time_ns()]
    cols = ["org", "namespace", "subject", "role", "granted_at", "granted_by", "version"]
    ch_client.insert("loom.namespace_grants", [row], column_names=cols)


@pytest.mark.asyncio
async def test_a_restricted_namespace_beside_star_reads_at_the_persons_grant(
    control_plane_url: str, ch_client: Any
) -> None:
    """Named beside `*`, a restricted namespace is read only through a direct grant to the
    token, capped at the person's own grant there: a viewer's token reads it and cannot
    write it, while it still writes its home. Without a grant of their own the mint
    refuses."""
    from server.auth.jwt import mint_token

    if not await _auth_on(control_plane_url):
        pytest.skip("server runs with auth off: every namespace is readable")
    org = unique_org("mintr")
    person, other = f"mint-{uuid.uuid4().hex[:8]}", f"mint-{uuid.uuid4().hex[:8]}"
    home, secret = f"home-{uuid.uuid4().hex[:6]}", f"priv-{uuid.uuid4().hex[:6]}"
    _restricted_namespace(ch_client, org, secret)
    _grant(ch_client, org, home, person, "writer")
    _grant(ch_client, org, secret, person, "viewer")
    _grant(ch_client, org, home, other, "writer")

    def token_of(subject: str) -> str:
        return mint_token(
            subject=subject, org=org, namespaces=[home], roles=["WRITER"], is_service_account=False
        )

    body = {"namespaces": [home, "*", secret]}
    refused = await _post(control_plane_url, token_of(other), body)
    assert refused.status_code == 403, refused.text
    assert secret in refused.json()["detail"]["message"]

    r = await _post(control_plane_url, token_of(person), body)
    assert r.status_code == 200, r.text
    minted = r.json()
    assert minted["restricted_namespaces"] == [secret]
    assert minted["saves_to"] == home
    rows = ch_client.query(
        "SELECT role FROM loom.namespace_grants FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}",
        parameters={"org": org, "ns": secret, "sub": minted["sa_subject"]},
    ).result_rows
    assert rows == [("viewer",)]

    token = minted["token"]
    read = {"namespace": secret, "agent": "it", "memory_key": "absent"}

    async def _reads() -> dict | None:
        # The new grant reaches the gate within the grant cache TTL.
        result = (
            await _mcp_post(control_plane_url, _tool_call("loom_memory_get", read), token)
        ).json()
        return None if result["result"]["isError"] else result

    await eventually(_reads, description="the token reads the restricted namespace")
    write = {**read, "memory_type": "semantic", "value": {"x": 1}}
    r = await _mcp_post(control_plane_url, _tool_call("loom_memory_set", write), token)
    assert r.json()["result"]["isError"] is True
    await _assert_unaddressed_write_lands_in(control_plane_url, token, home)
