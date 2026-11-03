"""A call whose `org` or `namespace` is absent to the gates (blank or the "-"
sentinel) runs under the target the gate authorized, over HTTP and MCP alike:
the token's org, never the literal "-". Otherwise a token of one org writes
rows under org "-", and a token of any other org reads them back the same way."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token, verify_token
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings

_SEEN: list[tuple[str, str, str]] = []


async def _record(inp: Any) -> Any:
    _SEEN.append((type(inp).__name__, inp.org, inp.namespace))
    raise RuntimeError("recorded; the handler's answer is not under test")


@pytest.fixture()
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    from server.transport import http as http_mod

    ops = [dataclasses.replace(op, handler=_record) for op in ALL_OPERATIONS]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    _SEEN.clear()
    return _SEEN


def _post(op: str, body: dict[str, Any], role: str = "viewer") -> None:
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    tok = mint_token(
        subject="probe", org="globex", namespaces=["ns1"], roles=[role], ttl_seconds=300
    )
    TestClient(app, raise_server_exceptions=False).post(
        f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {tok}"}
    )


@pytest.mark.parametrize("org", ["-", "", "  "])
def test_an_http_read_with_an_absent_org_runs_under_the_tokens_org(
    seen: list[tuple[str, str, str]], org: str
) -> None:
    _post("memory.get", {"org": org, "namespace": "ns1", "agent": "a", "memory_key": "k"})
    assert seen == [("MemoryGetInput", "globex", "ns1")]


def test_an_http_write_with_an_absent_org_runs_under_the_tokens_org(
    seen: list[tuple[str, str, str]],
) -> None:
    _post(
        "memory.set",
        {"org": "-", "namespace": "ns1", "agent": "a", "memory_key": "k", "value": "v"},
        role="writer",
    )
    assert seen == [("MemorySetInput", "globex", "ns1")]


def test_an_absent_namespace_takes_the_authorized_namespace() -> None:
    """A code read naming namespace "-" is authorized against the code
    namespace, and now reads it too."""
    from server.code.api.models_search import CodeSearchInput
    from server.transport.gates import Target, with_target

    inp = CodeSearchInput(org="globex", namespace="-", query="q")
    target = Target(
        org="globex",
        namespace="code-shared",
        named_namespace=None,
        register_namespace="code-shared",
    )
    out = with_target(inp, target)
    assert (out.org, out.namespace) == ("globex", "code-shared")
    # A named namespace, or nothing resolved, leaves the input as sent.
    named = CodeSearchInput(org="globex", namespace="mine", query="q")
    assert with_target(named, target) is named
    unresolved = Target(org=None, namespace=None, named_namespace=None, register_namespace=None)
    assert with_target(inp, unresolved) is inp


@pytest.mark.parametrize("org", ["-", "  "])
def test_an_mcp_call_with_an_absent_org_runs_under_the_tokens_org(
    monkeypatch: pytest.MonkeyPatch, org: str
) -> None:
    import server.transport.mcp_call as mcp_call

    calls: list[str] = []

    async def _handler(inp: Any) -> Any:
        calls.append(inp.org)
        raise RuntimeError("recorded")

    tool = "loom_memory_get"
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call.TOOLS_BY_NAME[tool], handler=_handler),
    )
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    auth = verify_token(
        mint_token(
            subject="probe", org="globex", namespaces=["ns1"], roles=["viewer"], ttl_seconds=300
        )
    )
    args = {"org": org, "namespace": "ns1", "agent": "a", "memory_key": "k"}
    asyncio.run(mcp_call._call_tool(1, {"name": tool, "arguments": args}, auth=auth))
    assert calls == ["globex"]
