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
    with pytest.raises(SystemExit) as e:
        cli.main(["call", "memory.search", "--data", "[1]"])
    assert e.value.code == 2 and "JSON object" in capsys.readouterr().err


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
            await agent.account.create_token(["support"], name="ci", role="viewer")
            await agent.account.create_token()
            await agent.account.set_default_namespace("support")
            await agent.deployment.workers_status()
            await agent.deployment.reset_models("7")
            await agent.deployment.release_claim("acme", "squatted", "abuse")
            await agent.deployment.set_setting("memory_ask_recency_weight", 0.35)
            await agent.deployment.set_setting("memory_ask_recency_weight", 0.35, expected=0.2)
            await agent.deployment.reset_setting("memory_ask_recency_weight", expected=0.35)
            await agent.deployment.reset_setting("memory_ask_recency_weight")
        finally:
            await agent.close()

    asyncio.run(go())
    assert wire.seen == [
        (
            "POST",
            "/v1/me/service_accounts.create",
            {"namespaces": ["support"], "ttl_days": 90, "name": "ci", "role": "viewer"},
        ),
        ("POST", "/v1/me/service_accounts.create", {"namespaces": "*", "ttl_days": 90}),
        ("POST", "/v1/me/set-default-namespace", {"namespace": "support"}),
        ("GET", "/v1/admin/workers.status", None),
        ("POST", "/v1/admin/models/reset", {"expected_version": "7"}),
        (
            "POST",
            "/v1/admin/claim_release",
            {"org": "acme", "namespace": "squatted", "reason": "abuse"},
        ),
        ("POST", "/v1/admin/settings.set", {"name": "memory_ask_recency_weight", "value": 0.35}),
        (
            "POST",
            "/v1/admin/settings.set",
            {"name": "memory_ask_recency_weight", "value": 0.35, "expected": 0.2},
        ),
        (
            "POST",
            "/v1/admin/settings.set",
            {"name": "memory_ask_recency_weight", "clear": True, "expected": 0.35},
        ),
        ("POST", "/v1/admin/settings.set", {"name": "memory_ask_recency_weight", "clear": True}),
    ]


def test_the_generated_admin_client_has_a_method_per_route() -> None:
    from loom.admin_routes import AdminRoutes

    from scripts.codegen.sdk_py_admin_gen import admin_routes

    names = {n for n in dir(AdminRoutes) if not n.startswith("_")}
    assert names == {r.name for r in admin_routes()}


def test_the_generated_admin_client_sends_the_body_as_given(wire) -> None:
    async def go() -> None:
        agent = Agent("acme", "support", "t", base_url="http://loom.test", api_key="k")
        try:
            await agent.deployment.routes.settings_list({"prefix": "memory_ask_"})
            await agent.deployment.routes.workers_status()
        finally:
            await agent.close()

    asyncio.run(go())
    assert wire.seen == [
        ("POST", "/v1/admin/settings.list", {"prefix": "memory_ask_"}),
        ("GET", "/v1/admin/workers.status", None),
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


def test_a_route_call_never_leaves_the_server(wire) -> None:
    async def go(path: str) -> None:
        agent = Agent("acme", "support", "t", base_url="http://loom.test", api_key="k")
        try:
            await agent.transport.request("GET", path)
        finally:
            await agent.close()

    for elsewhere in ("https://other.host/x", "//other.host/x", "v1/health"):
        with pytest.raises(ValueError, match="not a path"):
            asyncio.run(go(elsewhere))
    assert wire.seen == []


def test_a_validation_failure_says_which_field(wire, capsys) -> None:
    wire.status, wire.body = (
        422,
        {
            "detail": {
                "code": "VALIDATION_FAILED",
                "message": "Input validation failed",
                "errors": [{"loc": ["body", "value"], "msg": "Field required"}],
            }
        },
    )
    assert (
        cli.main(["call", "memory.set", "memory_key=a/b/c", "--org", "acme", "--namespace", "n"])
        == 1
    )
    assert "body.value: Field required" in capsys.readouterr().err


def test_token_create_sends_the_namespaces_in_flag_order(wire) -> None:
    argv = ["token", "create", "--name", "ci", "--namespace-role", "support=writer"]
    argv += ["--namespace", "docs", "--namespace-role", "ops=viewer", "--ttl-days", "0"]
    assert cli.main(argv) == 0
    method, path, body = wire.seen[0]
    assert (method, path) == ("POST", "/v1/me/service_accounts.create")
    assert body["name"] == "ci" and body["ttl_days"] == 0
    assert body["namespaces"] == [
        {"namespace": "support", "role": "writer"},
        "docs",  # your own role there
        {"namespace": "ops", "role": "viewer"},
    ]


def test_token_list_and_revoke(wire) -> None:
    wire.body = {"rows": []}
    cli.main(["token", "list"])
    cli.main(["token", "revoke", "--name", "ci"])
    assert [(m, p) for m, p, _ in wire.seen] == [
        ("GET", "/v1/me/service_accounts"),
        ("POST", "/v1/me/service_accounts.revoke"),
    ]


def test_token_create_reads_everything_by_default_and_needs_an_ns_role_pair(wire) -> None:
    assert cli.main(["token", "create"]) == 0
    assert wire.seen[0][2] == {"namespaces": "*", "ttl_days": None}
    wire.seen.clear()
    with pytest.raises(SystemExit):
        cli.main(["token", "create", "--name", "ci", "--namespace-role", "support"])
    assert wire.seen == []  # the role itself is the server's to check


def test_token_takes_its_options_after_the_action(wire) -> None:
    argv = ["token", "create", "--name", "ci", "--namespace", "docs"]
    assert cli.main([*argv, "--server", "http://loom.test"]) == 0
    assert wire.seen[0][1] == "/v1/me/service_accounts.create"
