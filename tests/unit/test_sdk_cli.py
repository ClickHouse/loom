"""`loom call` and the SDK's account and admin clients, against a
MockTransport: what goes on the wire for an op, for a /path, and for the
routes outside the op registry."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

import httpx
import pytest
from loom import Agent, cli
from loom.errors import AuthInsufficient


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for var in ("LOOM_API_KEY", "LOOM_BASE_URL", "LOOM_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("loom.credentials.CREDENTIALS_FILE", tmp_path / "credentials")
    monkeypatch.setattr("loom.transport.TOKEN_FILE", tmp_path / "token")


def test_fields_are_text_unless_typed() -> None:
    assert cli._field("memory_key=2024") == ("memory_key", "2024")
    assert cli._field("query=deploy window") == ("query", "deploy window")
    assert cli._field("value=") == ("value", "")
    assert cli._field("top_k:=5") == ("top_k", 5)
    assert cli._field('tags:=["a","b"]') == ("tags", ["a", "b"])
    for bad in ("no-equals", "top_k:=five", ":=1"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli._field(bad)


def test_data_must_be_an_object(capsys) -> None:
    assert cli.main(["call", "memory.search", "--data", "[1]"]) == 1
    assert "JSON object" in capsys.readouterr().err


def test_fields_win_over_data() -> None:
    args = argparse.Namespace(data='{"a": 1, "b": 2}', fields=[("b", 3)])
    assert cli._body(args) == {"a": 1, "b": 3}
    assert cli._body(argparse.Namespace(data=None, fields=[])) is None


def test_list_names_every_op_with_its_role(capsys) -> None:
    assert cli.main(["call", "--list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any(line.split() == ["grants.approve", "admin"] for line in lines), lines[:5]


class _Wire:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.status, self.body = status, body if body is not None else {"ok": True}
        self.seen: list[tuple[str, str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, sent))
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def wire(monkeypatch) -> _Wire:
    fake = _Wire()
    real = httpx.AsyncClient

    def client(*args: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(fake)
        kw.pop("http2", None)
        return real(*args, **kw)

    monkeypatch.setattr("loom.transport.httpx.AsyncClient", client)
    return fake


def test_an_op_is_posted_with_the_identity_filled_in(wire, capsys) -> None:
    assert (
        cli.main(["call", "grants.approve", "namespace=support", "subject=bob", "--org", "acme"])
        == 0
    )
    method, path, body = wire.seen[0]
    assert (method, path) == ("POST", "/v1/grants.approve")
    assert body["org"] == "acme" and body["namespace"] == "support" and body["subject"] == "bob"
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_a_path_is_sent_as_given(wire) -> None:
    cli.main(["call", "/v1/me/service_accounts"])
    cli.main(["call", "/v1/me/service_accounts.revoke", "name=ci"])
    assert wire.seen == [
        ("GET", "/v1/me/service_accounts", None),
        ("POST", "/v1/me/service_accounts.revoke", {"name": "ci"}),
    ]


def test_a_refusal_is_one_line_on_stderr_and_exit_1(wire, capsys) -> None:
    wire.status, wire.body = 403, {"detail": {"code": "AUTH_INSUFFICIENT", "message": "nope"}}
    assert cli.main(["call", "/v1/admin/workers.status"]) == 1
    assert capsys.readouterr().err.count("AUTH_INSUFFICIENT") == 1


def test_account_and_admin_clients_reach_the_routes_outside_the_registry(wire) -> None:
    async def go() -> None:
        agent = Agent("acme", "support", "t", base_url="http://loom.test", api_key="k")
        try:
            await agent.account.create_service_account("ci", ["support"], role="viewer")
            await agent.account.mcp_config("cursor", namespace="support")
            await agent.account.set_default_namespace("support")
            await agent.deployment.workers_status()
            await agent.deployment.reset_models("7")
            await agent.deployment.release_claim("acme", "squatted", "abuse")
        finally:
            await agent.close()

    asyncio.run(go())
    assert wire.seen == [
        (
            "POST",
            "/v1/me/service_accounts.create",
            {"name": "ci", "namespaces": ["support"], "ttl_days": 90, "role": "viewer"},
        ),
        (
            "POST",
            "/v1/me/setup/mcp_config",
            {"client": "cursor", "persist": False, "namespace": "support"},
        ),
        ("POST", "/v1/me/set-default-namespace", {"namespace": "support"}),
        ("GET", "/v1/admin/workers.status", None),
        ("POST", "/v1/admin/models/reset", {"expected_version": "7"}),
        (
            "POST",
            "/v1/admin/claim_release",
            {"org": "acme", "namespace": "squatted", "reason": "abuse"},
        ),
    ]


def test_route_errors_are_the_typed_sdk_errors(wire) -> None:
    wire.status, wire.body = (
        403,
        {"detail": {"code": "AUTH_INSUFFICIENT", "message": "admins only"}},
    )

    async def go() -> None:
        agent = Agent("acme", "support", "t", base_url="http://loom.test", api_key="k")
        try:
            await agent.deployment.models()
        finally:
            await agent.close()

    with pytest.raises(AuthInsufficient):
        asyncio.run(go())


def test_options_may_come_before_the_fields(wire) -> None:
    assert (
        cli.main(["call", "memory.get", "--namespace", "ns1", "memory_key=a/b/c", "--org", "acme"])
        == 0
    )
    _, path, body = wire.seen[0]
    assert path == "/v1/memory.get" and body["memory_key"] == "a/b/c" and body["namespace"] == "ns1"


def test_an_unknown_operation_points_at_the_list(capsys) -> None:
    assert cli.main(["call", "memory.nosuch"]) == 2
    assert "--list" in capsys.readouterr().err


def test_a_stray_option_is_still_an_error() -> None:
    with pytest.raises(SystemExit):
        cli.main(["call", "memory.get", "--bogus", "x"])
