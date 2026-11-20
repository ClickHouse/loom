"""Provider consent stays in the browser; the CLI reads only the own-account link."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from loom import cli
from loom.errors import AuthInsufficient


class _Wire:
    def __init__(self) -> None:
        self.status = 200
        self.body: Any = {"connections": []}
        self.seen: list[httpx.Request] = []
        self.clients: list[httpx.AsyncClient] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/v1/connection.list"
        self.seen.append(request)
        return httpx.Response(self.status, json=self.body)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Wire:
    for name in ("LOOM_PROFILE", "LOOM_BASE_URL", "LOOM_MENTOR_BRIEF", "LOOM_AUTO_RUN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOOM_API_KEY", "private-cli-bearer")
    monkeypatch.setattr("loom.credentials.CREDENTIALS_FILE", tmp_path / "credentials")
    monkeypatch.setattr("loom.transport.TOKEN_FILE", tmp_path / "token")
    monkeypatch.setattr("loom.credentials.signed_in_server", lambda *_a: "https://loom.test")
    fake = _Wire()
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake)
        kwargs.pop("http2", None)
        made = real_client(*args, **kwargs)
        fake.clients.append(made)
        return made

    monkeypatch.setattr("loom.transport.httpx.AsyncClient", client)

    def no_browser(*_args: Any, **_kwargs: Any) -> bool:
        raise AssertionError("the user starts consent in their browser")

    monkeypatch.setattr("webbrowser.open", no_browser)
    return fake


@pytest.mark.parametrize(
    "provider,kind", [("google", "gdrive"), ("notion", "notion"), ("linear", "linear")]
)
@pytest.mark.parametrize("connected", [False, True])
def test_connect_reads_the_own_provider_link_and_closes_client(
    wire, capsys, provider, kind, connected
) -> None:
    url = f"https://dashboard.test/settings/connected-accounts?kind={kind}"
    wire.body = {
        "connections": [
            {"kind": "other", "connect_url": "https://unrelated.test"},
            {
                "kind": kind,
                "connected": connected,
                "connect_url": url,
                "account_email": "private-account@example.test",
                "scopes": ["private-scope"],
                "credential": "private-response-secret",
            },
        ]
    }
    assert cli.main(["connect", provider]) == 0
    captured = capsys.readouterr()
    assert url in captured.out and "choose Connect or Reconnect" in captured.out
    assert "connected" not in captured.out.replace("connected-accounts", "")
    assert captured.err == ""
    for private in (
        "private-cli-bearer",
        "private-account",
        "private-scope",
        "private-response-secret",
    ):
        assert private not in captured.out
    assert len(wire.seen) == 1
    request = wire.seen[0]
    assert request.url.host == "loom.test"
    assert request.headers["Authorization"] == "Bearer private-cli-bearer"
    body = json.loads(request.content)
    assert body["agent"] == "loom-cli" and body["org"] == ""
    assert not {"kind", "subject", "credential", "return_to"} & body.keys()
    assert wire.clients and all(client.is_closed for client in wire.clients)


def test_connect_uses_the_explicit_server_before_environment(wire, monkeypatch) -> None:
    monkeypatch.setenv("LOOM_BASE_URL", "https://environment.test")
    wire.body = {
        "connections": [
            {
                "kind": "gdrive",
                "connect_url": "https://public.test/settings/connected-accounts?kind=gdrive",
            }
        ]
    }
    assert cli.main(["connect", "google", "--server", "https://explicit.test"]) == 0
    assert wire.seen[0].url.host == "explicit.test"


def test_connect_uses_the_normal_profile_credentials(wire, monkeypatch, capsys) -> None:
    seen = []

    class Login(httpx.Auth):
        def __init__(self, profile: str, server: str) -> None:
            self.profile = profile
            seen.append((profile, server))

        def auth_flow(self, request):
            request.headers["Authorization"] = "Bearer private-profile-bearer"
            yield request

    monkeypatch.setattr("loom.agent.StoredLogin", Login)
    monkeypatch.setattr("loom.agent.signed_in_server", lambda _profile: "https://profile.test")
    wire.body = {
        "connections": [
            {
                "kind": "notion",
                "connect_url": "https://public.test/settings/connected-accounts?kind=notion",
            }
        ]
    }
    assert cli.main(["connect", "notion", "--profile", "work"]) == 0
    assert seen == [("work", "https://profile.test")]
    assert wire.seen[0].url.host == "profile.test"
    assert wire.seen[0].headers["Authorization"] == "Bearer private-profile-bearer"
    assert "private-" not in capsys.readouterr().out


def test_connect_preserves_typed_refusal_and_closes_client(wire) -> None:
    wire.status = 403
    wire.body = {"detail": {"code": "AUTH_INSUFFICIENT", "message": "private-refusal"}}
    args = argparse.Namespace(provider="google", server=None, profile=None)
    with pytest.raises(AuthInsufficient):
        asyncio.run(cli._connect(args))
    assert wire.clients and all(client.is_closed for client in wire.clients)


def test_connect_refusal_prints_no_server_secrets_or_consent_link(wire, capsys) -> None:
    wire.status = 403
    wire.body = {
        "detail": {
            "code": "AUTH_INSUFFICIENT",
            "message": "private-cli-bearer",
            "errors": [{"msg": "private-code-and-state"}],
        }
    }
    assert cli.main(["connect", "google"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "loom login" in captured.err
    assert "private-" not in captured.err
    assert wire.clients and all(client.is_closed for client in wire.clients)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:private-secret",
        "https://user:private-secret@public.test/settings/connected-accounts?kind=gdrive",
        "https://public.test/v1/connect/gdrive/callback?state=private-secret",
        "https://public.test/settings/connected-accounts?kind=gdrive&code=private-secret",
        "https://public.test/settings/connected-accounts?kind=notion",
        "https://public.test/settings/connected-accounts?kind=gdrive#private-secret",
        "https://public.test/settings/connected-accounts?kind=gdrive\nprivate-secret",
        "https://public.test:bad/settings/connected-accounts?kind=gdrive",
    ],
)
def test_connect_rejects_links_that_carry_other_material(wire, capsys, url) -> None:
    wire.body = {"connections": [{"kind": "gdrive", "connect_url": url}]}
    assert cli.main(["connect", "google"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "private-secret" not in captured.err
    assert wire.clients and all(client.is_closed for client in wire.clients)


@pytest.mark.parametrize(
    "connections",
    [None, [], [{"kind": "gdrive"}], ["gdrive"], [{"kind": "gdrive"}, {"kind": "gdrive"}]],
)
def test_connect_refuses_missing_or_ambiguous_provider_links(wire, capsys, connections) -> None:
    wire.body = {"connections": connections}
    assert cli.main(["connect", "google"]) == 1
    assert capsys.readouterr().out == ""
    assert wire.clients and all(client.is_closed for client in wire.clients)


def test_connect_refuses_an_invalid_response_shape(wire, capsys) -> None:
    wire.body = ["private-response-secret"]
    assert cli.main(["connect", "google"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "private-response-secret" not in captured.err
    assert wire.clients and all(client.is_closed for client in wire.clients)


def test_server_cli_delegates_connect_before_loading_deployment_environment(monkeypatch) -> None:
    from server.cli import app

    def no_dotenv() -> None:
        raise AssertionError("connect uses the SDK session, not the deployment environment")

    seen = []
    monkeypatch.setattr(app, "_load_dotenv_like_the_container", no_dotenv)
    monkeypatch.setattr(app, "run_sdk_command", lambda argv: seen.append(argv) or 0)
    argv = ["connect", "linear", "--profile", "work"]
    assert app.main(argv) == 0 and seen == [argv]


def test_unknown_provider_is_a_parser_error_before_any_request(wire) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["connect", "unknown"])
    assert error.value.code == 2 and wire.seen == []
