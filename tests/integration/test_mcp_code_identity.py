"""MCP tools/call must NOT inject the caller's memory-namespace defaults
into code.* ops.

GATE: a chat-surface code tool call carries only its action args; the code
handlers default an empty org/namespace to the deployment's code namespace
(resolve_code_identity), and authz for a grant-less human token passes on
the flat-role path only while the namespace stays absent. Injecting the
caller's saved/personal namespace would point every human code.search at an
empty personal index (permanent abstain).

Wire-observable (the CI server runs out-of-process): code.index_status
echoes the namespace it resolved, so a bare call must come back with the
DEPLOYMENT code namespace, not the caller's personal one.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


async def _call(base_url: str, tool: str, arguments: dict) -> dict:
    from server.auth.jwt import mint_token

    tok = mint_token(
        subject="mcp-code-identity-test",
        org=os.environ.get("LOOM_CODE_ORG") or "ci",
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=300,
    )
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as c:
        r = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200, f"/mcp returned {r.status_code}: {r.text}"
        return r.json()


@pytest.mark.asyncio
async def test_bare_code_call_resolves_to_deployment_code_namespace(
    control_plane_url: str,
) -> None:
    expected_org = os.environ.get("LOOM_CODE_ORG") or pytest.skip(
        "LOOM_CODE_ORG unset: code indexing is not configured in this deployment"
    )
    expected = os.environ.get("LOOM_CODE_NAMESPACE") or pytest.skip(
        "LOOM_CODE_NAMESPACE unset: no deployment code namespace to resolve to"
    )
    resp = await _call(control_plane_url, "loom_code_index_status", {})
    assert "error" not in resp, resp
    payload = json.loads(resp["result"]["content"][0]["text"])
    # The deployment default (LOOM_CODE_NAMESPACE, which CI sets), never the
    # caller default `personal-mcp-code-identity-test`.
    assert payload["org"] == expected_org, payload
    assert payload["namespace"] == expected, payload
    assert not payload["namespace"].startswith("personal-")


@pytest.mark.asyncio
async def test_memory_op_still_gets_identity_defaults(
    control_plane_url: str,
) -> None:
    """Control: non-code ops keep the convenience injection. memory.list
    requires org+namespace, so a bare call only validates if the MCP layer
    injected them."""
    resp = await _call(control_plane_url, "loom_memory_list", {"limit": 1})
    assert "error" not in resp, resp
