"""A single-user server's HTTP call with no token, to one of the everyday memory and
search ops, runs under the install's identity where it names none: the org and
namespace MCP and the dashboard use, and the op's own default agent. Every other
op, any token, and every other identity mode names its own identity as before."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.settings import settings
from server.transport.gates import SINGLE_USER_DEFAULT_AGENT
from tests.unit.ch_fake import RecordingCH

Seen = list[tuple[str, str, str, str | None]]
_SEEN: Seen = []


async def _record(inp: Any) -> Any:
    _SEEN.append((type(inp).__name__, inp.org, inp.namespace, getattr(inp, "agent", None)))
    raise RuntimeError("recorded; the handler's answer is not under test")


@pytest.fixture()
def seen(monkeypatch: pytest.MonkeyPatch) -> Seen:
    from server.transport import http as http_mod

    ops = [dataclasses.replace(op, handler=_record) for op in ALL_OPERATIONS]
    monkeypatch.setattr(http_mod, "ALL_OPERATIONS", ops)
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    monkeypatch.setattr(settings, "default_namespace", "local")
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: None)
    RecordingCH(strict=False).install(monkeypatch)
    _SEEN.clear()
    return _SEEN


def _post(op: str, body: dict[str, Any], token: str = "") -> Any:
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app, raise_server_exceptions=False).post(
        f"/v1/{op}", json=body, headers=headers
    )


def _missing(response: Any) -> set[str]:
    assert response.status_code == 422, response.text
    return {e["loc"][-1] for e in response.json()["detail"]["errors"]}


def _default_agent(op: str) -> Any:
    return OPERATIONS_BY_NAME[op].input_model.model_fields["agent"].default


def test_the_quickstart_calls_need_only_their_own_fields(seen: Seen) -> None:
    _post("memory.set", {"memory_key": "deploy_window", "value": "Tue and Thu only."})
    _post("memory.get", {"memory_key": "deploy_window"})
    _post("memory.search", {"query": "when can we deploy?"})
    assert seen == [
        ("MemorySetInput", "dev", "local", "http"),
        ("MemoryGetInput", "dev", "local", "http"),
        ("MemorySearchInput", "dev", "local", _default_agent("memory.search")),
    ]


@pytest.mark.parametrize("op", ["memory.list", "memory.history", "memory.related"])
def test_a_list_or_a_key_history_reads_every_agent(seen: Seen, op: str) -> None:
    """Their agent is required, and filled with `*`, as MCP fills it: what another
    agent wrote is found."""
    _post(op, {"memory_key": "deploy_window"})
    assert len(seen) == 1 and seen[0][1:] == ("dev", "local", "*")


@pytest.mark.parametrize("blank", ["", "-", "  "])
def test_a_blank_or_sentinel_identity_is_filled_too(seen: Seen, blank: str) -> None:
    """What the SDKs send for an identity their caller left empty."""
    _post("memory.get", {"org": blank, "namespace": blank, "agent": "a", "memory_key": "k"})
    assert seen == [("MemoryGetInput", "dev", "local", "a")]


def test_an_identity_the_call_names_is_kept(seen: Seen) -> None:
    _post(
        "memory.set",
        {"org": "dev", "namespace": "scratch", "agent": "bot", "memory_key": "k", "value": "v"},
    )
    assert seen == [("MemorySetInput", "dev", "scratch", "bot")]


def test_the_namespace_is_the_installs_default_namespace(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "default_namespace", "home")
    _post("memory.set", {"memory_key": "k", "value": "v"})
    assert seen == [("MemorySetInput", "dev", "home", "http")]


@pytest.mark.parametrize(
    ("op", "body", "named"),
    [
        ("namespace.delete", {}, {"org", "namespace"}),
        ("memory.clear", {}, {"org", "namespace"}),
        ("memory.lock", {"memory_key": "k", "reason": "x"}, {"org", "namespace", "agent"}),
        ("memory.bulk_get", {"memory_keys": ["k"]}, {"org", "namespace", "agent"}),
        ("working_memory.set", {"content": "c"}, {"org", "namespace", "agent"}),
        ("tracker.item", {"number": 1}, {"namespace"}),
    ],
)
def test_any_other_op_still_names_its_identity(
    seen: Seen, op: str, body: dict[str, Any], named: set[str]
) -> None:
    """Admin and namespace ops, exact-identity reads and writes, working memory and
    the plane ops: what was left out is still missing, never the install's."""
    assert named <= _missing(_post(op, body))
    assert seen == []


def test_every_filled_op_takes_the_fields_it_fills() -> None:
    """Each op on the list takes org and namespace, and an agent of `http` or `*` only
    where it requires one."""
    for name, agent in SINGLE_USER_DEFAULT_AGENT.items():
        fields = OPERATIONS_BY_NAME[name].input_model.model_fields
        assert {"org", "namespace"} <= set(fields), name
        required_agent = "agent" in fields and fields["agent"].is_required()
        assert bool(agent) == required_agent, name


def test_a_call_with_a_token_names_its_own_identity(seen: Seen) -> None:
    """A token is a caller of its own, so nothing is filled for it."""
    token = mint_token(
        subject="probe", org="dev", namespaces=["local"], roles=["writer"], ttl_seconds=300
    )
    assert {"namespace", "agent"} <= _missing(
        _post("memory.set", {"memory_key": "k", "value": "v"}, token=token)
    )
    assert seen == []


def test_another_identity_mode_without_a_token_is_not_filled(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth off under another identity provider has no single install identity."""
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    _missing(_post("memory.set", {"memory_key": "k", "value": "v"}))
    assert seen == []


def test_a_server_with_sign_in_refuses_a_call_with_no_token(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "auth_required", True)
    assert _post("memory.set", {"memory_key": "k", "value": "v"}).status_code == 401
    assert seen == []


def test_a_token_call_naming_no_namespace_is_refused(
    seen: Seen, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `Agent(api_key=...)` with no namespace sends to a server with sign-in
    (the SDK leaves an empty namespace out): nothing fills it, and it is a 422."""
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "auth_required", True)
    token = mint_token(
        subject="probe", org="dev", namespaces=["local"], roles=["writer"], ttl_seconds=300
    )
    body = {"org": "", "agent": "bot", "memory_key": "k", "value": "v"}
    assert "namespace" in _missing(_post("memory.set", body, token=token))
    assert seen == []


def test_the_http_reference_names_every_filled_op() -> None:
    """The op list in the HTTP reference is the code's list."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "docs/reference/http-api.md").read_text()
    identity = page[page.index("- **Identity.**") : page.index("- **`consumer`.**")]
    assert [op for op in SINGLE_USER_DEFAULT_AGENT if f"`{op}`" not in identity] == []
