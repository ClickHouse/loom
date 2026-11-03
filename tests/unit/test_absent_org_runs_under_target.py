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
from tests.unit.ch_fake import RecordingCH

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
    RecordingCH(strict=False).install(monkeypatch)
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
    """A code read naming namespace "-" reads the code namespace it is
    authorized against."""
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


def test_a_tracker_read_with_an_absent_org_takes_the_tracker_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tracker_org", "trackers")
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.tracker.api.models import TrackerItemInput
    from server.transport.gates import authz_target, with_target

    op = OPERATIONS_BY_NAME["tracker.item"]
    inp = TrackerItemInput.model_construct(org="-", namespace="-")
    target = authz_target(op, org=inp.org, namespace=inp.namespace, auth=None)
    out = with_target(inp, target)
    # The org takes the tracker plane's; a tracker namespace has no plane default.
    assert (out.org, out.namespace) == ("trackers", "-")


def test_an_org_wide_token_naming_a_dash_org_with_a_namespace_is_incomplete() -> None:
    """An org-`*` token has no single org for "-" to stand for: a namespaced
    call is refused rather than run under the literal "-"."""
    from server.core.errors import IdentityIncompleteError
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.gates import authz_target

    auth = verify_token(
        mint_token(subject="ops", org="*", namespaces=["*"], roles=["admin"], ttl_seconds=300)
    )
    with pytest.raises(IdentityIncompleteError):
        authz_target(OPERATIONS_BY_NAME["memory.get"], org="-", namespace="ns1", auth=auth)


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
    RecordingCH(strict=False).install(monkeypatch)
    auth = verify_token(
        mint_token(
            subject="probe", org="globex", namespaces=["ns1"], roles=["viewer"], ttl_seconds=300
        )
    )
    args = {"org": org, "namespace": "ns1", "agent": "a", "memory_key": "k"}
    asyncio.run(mcp_call._call_tool(1, {"name": tool, "arguments": args}, auth=auth))
    assert calls == ["globex"]


def test_me_with_an_absent_org_names_the_callers_own_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP maps `@me` before the gate; an absent org there is the token's."""
    from server.auth import gateway
    from server.memory.api.models_crud import MemoryGetInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import http as http_mod

    orgs: list[str] = []

    def _personal(auth: Any, org: str, namespace: str) -> str:
        orgs.append(org)
        return f"personal-of-{org}"

    monkeypatch.setattr(gateway, "personal_namespace", _personal)
    auth = verify_token(
        mint_token(
            subject="probe", org="globex", namespaces=["*"], roles=["viewer"], ttl_seconds=300
        )
    )
    body = {"org": "-", "namespace": "@me", "agent": "a", "memory_key": "k"}
    payload, raw = http_mod._resolve_personal(
        OPERATIONS_BY_NAME["memory.get"],
        None,  # type: ignore[arg-type]
        MemoryGetInput(**body),
        body,
        auth,
    )
    assert orgs == ["globex"]
    assert payload.namespace == raw["namespace"] == "personal-of-globex"


def test_the_mentor_hint_is_keyed_on_the_admitted_org(
    seen: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.insights.app import mentor
    from server.memory.api.models_crud import MemoryGetOutput
    from server.transport import http as http_mod

    async def _answer(inp: Any) -> MemoryGetOutput:
        return MemoryGetOutput(found=False)

    ops = [
        dataclasses.replace(op, handler=_answer) if op.name == "memory.get" else op
        for op in ALL_OPERATIONS
    ]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    keyed: list[tuple[str, str, str]] = []

    def _hint(*_a: Any, org: str, namespace: str, session_key: str, **_k: Any) -> None:
        keyed.append((org, namespace, session_key))

    monkeypatch.setattr(mentor, "call_hint", _hint)
    _post("memory.get", {"org": "-", "namespace": "ns1", "agent": "a", "memory_key": "k"})
    assert keyed == [("globex", "ns1", "globex/ns1/a")]


def test_a_call_naming_no_namespace_gives_the_mentor_none(
    seen: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent namespace stays absent for the mentor, which then reads no
    profile, rather than becoming the literal "-"."""
    from server.identity.api.models import NamespaceListMineOutput
    from server.insights.app import mentor
    from server.transport import http as http_mod

    async def _answer(inp: Any) -> NamespaceListMineOutput:
        return NamespaceListMineOutput(rows=[])

    ops = [
        dataclasses.replace(op, handler=_answer) if op.name == "namespace.list_mine" else op
        for op in ALL_OPERATIONS
    ]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    keyed: list[tuple[str, str]] = []

    def _hint(*_a: Any, org: str, namespace: str, **_k: Any) -> None:
        keyed.append((org, namespace))

    monkeypatch.setattr(mentor, "call_hint", _hint)
    _post("namespace.list_mine", {"org": "globex"})
    assert keyed == [("globex", "")]
