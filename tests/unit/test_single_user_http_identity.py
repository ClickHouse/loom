"""A single-user server's HTTP call with no token runs under the install's identity
where it names none: the org and namespace MCP and the dashboard use, and the
agent `http` where the op needs one. A token, or any other identity mode, names
its own identity as before."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token
from server.operations.registry import ALL_OPERATIONS
from server.settings import settings
from tests.unit.ch_fake import RecordingCH

_SEEN: list[tuple[str, str, str | None, str]] = []


async def _record(inp: Any) -> Any:
    namespace = getattr(inp, "namespace", None)
    _SEEN.append((type(inp).__name__, inp.org, namespace, getattr(inp, "agent", "")))
    raise RuntimeError("recorded; the handler's answer is not under test")


@pytest.fixture()
def seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str | None, str]]:
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


def test_a_write_naming_only_its_key_and_value_runs_under_the_install(
    seen: list[tuple[str, str, str | None, str]],
) -> None:
    _post("memory.set", {"memory_key": "deploy_window", "value": "Tue and Thu only."})
    assert seen == [("MemorySetInput", "dev", "local", "http")]


def test_a_search_naming_no_agent_still_reads_every_agent(
    seen: list[tuple[str, str, str | None, str]],
) -> None:
    """memory.search's agent is optional: left out, it reads every agent's rows."""
    _post("memory.search", {"query": "when can we deploy?"})
    assert len(seen) == 1
    name, org, namespace, agent = seen[0]
    assert (name, org, namespace) == ("MemorySearchInput", "dev", "local")
    assert agent != "http"


@pytest.mark.parametrize("blank", ["", "-", "  "])
def test_a_blank_or_sentinel_identity_is_filled_too(
    seen: list[tuple[str, str, str | None, str]], blank: str
) -> None:
    """What the SDKs send for an identity their caller left empty."""
    _post("memory.get", {"org": blank, "namespace": blank, "agent": "a", "memory_key": "k"})
    assert seen == [("MemoryGetInput", "dev", "local", "a")]


def test_an_identity_the_call_names_is_kept(seen: list[tuple[str, str, str | None, str]]) -> None:
    _post(
        "memory.set",
        {"org": "dev", "namespace": "scratch", "agent": "bot", "memory_key": "k", "value": "v"},
    )
    assert seen == [("MemorySetInput", "dev", "scratch", "bot")]


def test_the_namespace_is_the_installs_default_namespace(
    seen: list[tuple[str, str, str | None, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "default_namespace", "home")
    _post("memory.set", {"memory_key": "k", "value": "v"})
    assert seen == [("MemorySetInput", "dev", "home", "http")]


def test_a_call_with_a_token_names_its_own_identity(
    seen: list[tuple[str, str, str | None, str]],
) -> None:
    """A token is a caller of its own, so nothing is filled for it."""
    token = mint_token(
        subject="probe", org="dev", namespaces=["local"], roles=["writer"], ttl_seconds=300
    )
    response = _post("memory.set", {"memory_key": "k", "value": "v"}, token=token)
    assert response.status_code == 422
    assert {e["loc"][-1] for e in response.json()["detail"]["errors"]} >= {"namespace", "agent"}
    assert seen == []


def test_another_identity_mode_without_a_token_is_not_filled(
    seen: list[tuple[str, str, str | None, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth off under another identity provider has no single install identity."""
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    response = _post("memory.set", {"memory_key": "k", "value": "v"})
    assert response.status_code == 422
    assert seen == []


def test_a_server_with_sign_in_refuses_a_call_with_no_token(
    seen: list[tuple[str, str, str | None, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "auth_required", True)
    assert _post("memory.set", {"memory_key": "k", "value": "v"}).status_code == 401
    assert seen == []


def test_an_org_wide_read_stays_org_wide(seen: list[tuple[str, str, str | None, str]]) -> None:
    """An optional namespace left out reads every namespace: the dashboard's audit,
    cost and health views send none on purpose."""
    _post("audit.list", {})
    assert seen == [("AuditListInput", "dev", None, "")]


def test_a_tracker_op_keeps_its_plane_default(
    seen: list[tuple[str, str, str | None, str]],
) -> None:
    """A tracker op has its own org and no namespace to fall back on: nothing is filled,
    so the missing namespace is still named."""
    response = _post("tracker.item", {"number": 1})
    assert response.status_code == 422
    assert "namespace" in {e["loc"][-1] for e in response.json()["detail"]["errors"]}
    assert seen == []


def test_a_token_call_naming_no_namespace_is_refused(
    seen: list[tuple[str, str, str | None, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """What `Agent(api_key=...)` with no namespace sends to a server with sign-in
    (the SDK leaves an empty namespace out): nothing fills it, and it is a 422."""
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(settings, "auth_required", True)
    token = mint_token(
        subject="probe", org="dev", namespaces=["local"], roles=["writer"], ttl_seconds=300
    )
    body = {"org": "", "agent": "bot", "memory_key": "k", "value": "v"}
    response = _post("memory.set", body, token=token)
    assert response.status_code == 422, response.text
    assert "namespace" in {e["loc"][-1] for e in response.json()["detail"]["errors"]}
    assert seen == []
