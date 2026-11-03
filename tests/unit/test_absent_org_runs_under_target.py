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


def _resolved(op: Any, body: dict[str, Any]) -> dict[str, Any]:
    from server.transport.gates import with_resolved_org

    resolved: dict[str, Any] = with_resolved_org(op, body, None)
    return resolved


def _post(op: str, body: dict[str, Any], role: str = "viewer") -> Any:
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    tok = mint_token(
        subject="probe", org="globex", namespaces=["ns1"], roles=[role], ttl_seconds=300
    )
    return TestClient(app, raise_server_exceptions=False).post(
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


def test_an_absent_namespace_takes_the_authorized_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code read naming namespace "-" reads the code namespace it is
    authorized against."""
    from server.code.api.models_search import CodeSearchInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.gates import Target, authz_target, with_target

    monkeypatch.setattr(settings, "code_org", "globex")
    monkeypatch.setattr(settings, "code_namespace", "code-shared")
    op = OPERATIONS_BY_NAME["code.search"]
    inp = CodeSearchInput(org="globex", namespace="-", query="q")
    target = authz_target(op, org=inp.org, namespace=inp.namespace, auth=None)
    out = with_target(inp, target)
    assert (out.org, out.namespace) == ("globex", "code-shared")
    # A named namespace, or nothing resolved, leaves the input as sent.
    named = CodeSearchInput(org="globex", namespace="mine", query="q")
    assert with_target(named, target) is named
    unresolved = Target(org=None, namespace=None, named_namespace=None, register_namespace=None)
    assert with_target(inp, unresolved) is inp
    # Fanned out over a list, a "-" namespace does not lead it either.
    fanned = CodeSearchInput(org="globex", namespace="-", namespaces=["a"], query="q")
    assert fanned.namespaces == ["a"]
    assert with_target(fanned, target).namespaces == ["a"]


def test_a_required_namespace_sent_as_dash_is_left_as_sent() -> None:
    """An admin write on a repository names its namespace: "-" must not
    become the shared code index."""
    from server.code.api.models_repos import CodeRepoSetAccessInput
    from server.transport.gates import Target, with_target

    assert CodeRepoSetAccessInput.model_fields["namespace"].is_required()
    inp = CodeRepoSetAccessInput.model_validate(
        {"org": "globex", "namespace": "-", "classification": "restricted", "join_policy": "closed"}
    )
    target = Target(
        org="globex",
        namespace="code-shared",
        named_namespace=None,
        register_namespace="code-shared",
    )
    assert with_target(inp, target).namespace == "-"


def test_a_code_admin_write_with_a_dash_org_takes_the_code_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code op's absent org is the code plane's, which the gate authorized
    the call against; the repository namespace it names is kept."""
    from server.code.api.models_repos import CodeRepoSetAccessInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.gates import authz_target, with_target

    monkeypatch.setattr(settings, "code_org", "code-org")
    op = OPERATIONS_BY_NAME["code.repo.set_access"]
    body = {
        "org": "-",
        "namespace": "repo@main",
        "classification": "restricted",
        "join_policy": "closed",
    }
    inp = CodeRepoSetAccessInput.model_validate(_resolved(op, body))
    target = authz_target(op, org=inp.org, namespace=inp.namespace, auth=None)
    out = with_target(inp, target)
    assert (out.org, out.namespace) == ("code-org", "repo@main")


def test_a_tracker_read_with_an_absent_org_takes_the_tracker_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "tracker_org", "trackers")
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.tracker.api.models import TrackerItemInput
    from server.transport.gates import authz_target, with_target

    op = OPERATIONS_BY_NAME["tracker.item"]
    body = {"org": "-", "namespace": "-", "numbers": [1]}
    inp = TrackerItemInput.model_validate(_resolved(op, body))
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


@pytest.mark.parametrize("org", ["-", "  ", " globex "])
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


def test_the_mcp_mentor_hint_is_keyed_on_the_admitted_org(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.transport.mcp_call as mcp_call
    from server.insights.app import mentor
    from server.memory.api.models_crud import MemoryGetOutput

    async def _answer(inp: Any) -> MemoryGetOutput:
        return MemoryGetOutput(found=False)

    tool = "loom_memory_get"
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call.TOOLS_BY_NAME[tool], handler=_answer),
    )
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    RecordingCH(strict=False).install(monkeypatch)
    keyed: list[tuple[str, str, str]] = []

    def _hint(*_a: Any, org: str, namespace: str, session_key: str, **_k: Any) -> None:
        keyed.append((org, namespace, session_key))

    monkeypatch.setattr(mentor, "call_hint", _hint)
    auth = verify_token(
        mint_token(
            subject="probe", org="globex", namespaces=["ns1"], roles=["viewer"], ttl_seconds=300
        )
    )
    args = {"org": "-", "namespace": "ns1", "agent": "a", "memory_key": "k"}
    asyncio.run(mcp_call._call_tool(1, {"name": tool, "arguments": args}, auth=auth))
    assert keyed == [("globex", "ns1", "globex/ns1/a")]


def _code_plane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_org", "code-org")
    monkeypatch.setattr(settings, "code_namespace", "code-shared")


def test_an_http_code_read_naming_no_tenant_reads_the_code_plane(
    seen: list[tuple[str, str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _code_plane(monkeypatch)
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    tok = mint_token(
        subject="probe", org="code-org", namespaces=["*"], roles=["viewer"], ttl_seconds=300
    )
    TestClient(app, raise_server_exceptions=False).post(
        "/v1/code.search",
        json={"org": "-", "namespace": "-", "query": "q"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert [(org, ns) for _, org, ns in seen] == [("code-org", "code-shared")]


def test_an_mcp_code_read_naming_no_tenant_reads_the_code_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.transport.mcp_call as mcp_call

    _code_plane(monkeypatch)
    calls: list[tuple[str, str]] = []

    async def _handler(inp: Any) -> Any:
        calls.append((inp.org, inp.namespace))
        raise RuntimeError("recorded")

    tool = "loom_code_search"
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
            subject="probe", org="code-org", namespaces=["*"], roles=["viewer"], ttl_seconds=300
        )
    )
    args = {"org": "-", "namespace": "-", "query": "q"}
    asyncio.run(mcp_call._call_tool(1, {"name": tool, "arguments": args}, auth=auth))
    assert calls == [("code-org", "code-shared")]


def _inbox(org: str, ref: str) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": "ns1",
        "inbox": "jobs",
        "owner_agent": "bot",
        "webhook_secret_ref": ref,
    }


def test_a_validator_reads_the_filled_org(
    seen: list[tuple[str, str, str]],
) -> None:
    """A validator that reads the org sees the one the call runs under: an
    inbox's webhook secret names it."""
    from server.core.webhook_secrets import webhook_secret_org_prefix

    own = f"{webhook_secret_org_prefix('globex')}HOOK"
    r = _post("inbox.register", _inbox("-", own), role="admin")
    assert seen == [("InboxRegisterInput", "globex", "ns1")], r.text


def test_a_filled_org_that_fails_validation_is_a_422(
    seen: list[tuple[str, str, str]],
) -> None:
    from server.core.webhook_secrets import webhook_secret_org_prefix
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    tok = mint_token(
        subject="probe", org="globex", namespaces=["ns1"], roles=["admin"], ttl_seconds=300
    )
    dash = f"{webhook_secret_org_prefix('-')}HOOK"
    r = TestClient(app, raise_server_exceptions=False).post(
        "/v1/inbox.register",
        json=_inbox("-", dash),
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "VALIDATION_FAILED"
    assert seen == []


def test_an_mcp_dash_org_takes_the_callers_default_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over MCP a "-" org is filled from the caller's defaults, as an empty one
    is: an org-wide token's call runs in its default org either way."""
    import server.transport.mcp_call as mcp_call

    calls: list[str] = []

    async def _handler(inp: Any) -> Any:
        calls.append(inp.org)
        raise RuntimeError("recorded")

    async def _defaults(auth: Any, client_name: Any = None) -> dict[str, str]:
        return {"org": "acme", "namespace": "priv", "agent": "claude-code"}

    tool = "loom_memory_get"
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        tool,
        dataclasses.replace(mcp_call.TOOLS_BY_NAME[tool], handler=_handler),
    )
    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    RecordingCH(strict=False).install(monkeypatch)
    auth = verify_token(
        mint_token(subject="ops", org="*", namespaces=["*"], roles=["admin"], ttl_seconds=300)
    )
    for org in ("", "-"):
        args = {"org": org, "agent": "a", "memory_key": "k"}
        asyncio.run(mcp_call._call_tool(1, {"name": tool, "arguments": args}, auth=auth))
    assert calls == ["acme", "acme"]


@pytest.mark.parametrize("org", [None, 0, []])
def test_a_null_or_non_string_org_is_still_refused(
    seen: list[tuple[str, str, str]], org: Any
) -> None:
    r = _post("memory.get", {"org": org, "namespace": "ns1", "agent": "a", "memory_key": "k"})
    assert r.status_code == 422, r.text
    assert seen == []


def test_a_padded_org_or_namespace_runs_stripped(seen: list[tuple[str, str, str]]) -> None:
    """The gate authorizes a padded name stripped; the handler gets it
    stripped too, or the row lands under a name an erasure never reaches."""
    body = {"org": " globex ", "namespace": " ns1 ", "agent": "a", "memory_key": "k", "value": "v"}
    _post("memory.set", body, role="writer")
    assert seen == [("MemorySetInput", "globex", "ns1")]


def test_the_ledger_records_a_padded_name_stripped() -> None:
    from server.transport._common import ledger_identity

    row = ledger_identity({"org": " globex ", "namespace": " ns1 "})
    assert (row["org"], row["namespace"]) == ("globex", "ns1")
