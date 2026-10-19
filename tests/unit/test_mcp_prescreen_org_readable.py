"""An org-readable code namespace is admitted the same way over MCP and HTTP.

The gate (`check_authorized`) lets every principal of the org read a code
namespace the deployment marks org-readable, with no grant and no claim.
The MCP pre-dispatch screen must not refuse what the gate admits: it asks
the gate whether the namespace is readable instead of testing the claim and
the grant itself.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from server.auth.authz import check_authorized
from server.auth.jwt import AuthInfo
from server.operations.base import Role
from server.transport.mcp_auth import _screen_tool_call

ORG, CODE_NS = "acme", "code-acme"


@dataclass(frozen=True)
class _Entry:
    classification: str = "standard"
    org_readable: bool = True


class _Directory:
    async def is_restricted(self, org: str, namespace: str) -> bool:
        return False

    def is_restricted_cached(self, org: str, namespace: str) -> bool:
        return False

    def code_entry(self, org: str, namespace: str) -> _Entry | None:
        return _Entry() if (org, namespace) == (ORG, CODE_NS) else None


@dataclass(frozen=True)
class _Repo:
    org: str
    namespace: str
    repo: str


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.directory as directory_mod
    import server.code.domain.index.repos as repos
    import server.tracker.domain.namespaces as tracker_ns

    async def _no_grant(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(directory_mod, "_directory", _Directory())
    monkeypatch.setattr(repos, "configured_repos", lambda: [_Repo(ORG, CODE_NS, "acme/acme")])
    monkeypatch.setattr(tracker_ns, "configured_namespaces", dict)
    monkeypatch.setattr("server.auth.authz.grant_role_for", _no_grant)
    monkeypatch.setattr("server.auth.service_accounts.inheriting_creator", _no_grant)


def _member() -> AuthInfo:
    return AuthInfo(
        subject="bob",
        org=ORG,
        namespaces=("personal-bob",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )


def _call() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "loom_code_search", "arguments": {"namespace": CODE_NS, "query": "x"}},
    }


def test_the_gate_admits_an_org_readable_code_namespace(wired: None) -> None:
    asyncio.run(check_authorized(_member(), Role.VIEWER, ORG, CODE_NS))


def test_the_gate_still_refuses_a_write_there(wired: None) -> None:
    with pytest.raises(PermissionError):
        asyncio.run(check_authorized(_member(), Role.WRITER, ORG, CODE_NS))


def test_the_mcp_screen_admits_what_the_gate_admits(wired: None) -> None:
    assert asyncio.run(_screen_tool_call(_call(), _member())) is None


def test_the_mcp_screen_refuses_another_orgs_token(wired: None) -> None:
    other = AuthInfo(
        subject="eve", org="other", namespaces=(), roles=(Role.WRITER,), is_service_account=False
    )
    req = _call()
    req["params"]["arguments"]["org"] = ORG
    out = asyncio.run(_screen_tool_call(req, other))
    assert out is not None and out["result"]["structuredContent"]["code"] == "ORG_MISMATCH"
