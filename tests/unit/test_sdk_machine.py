"""The SDK's bot login: where a CI job's OIDC token comes from, what it sends
to /oauth/token, and when it asks for a new hour-long token."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sdk" / "python"))

from loom import machine
from loom.errors import AuthInvalid


@pytest.fixture
def token_endpoint(monkeypatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    def post(url: str, *, data: dict[str, str], timeout: float) -> httpx.Response:
        assert url == "https://loom.example/oauth/token"
        sent.append(data)
        body = {"access_token": f"t{len(sent)}", "token_type": "Bearer", "expires_in": 3600}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(machine.httpx, "post", post)
    for var in ("LOOM_WORKLOAD_TOKEN_FILE", "ACTIONS_ID_TOKEN_REQUEST_URL"):
        monkeypatch.delenv(var, raising=False)
    return sent


def test_a_ci_token_from_a_file_is_exchanged(token_endpoint, tmp_path, monkeypatch) -> None:
    (tmp_path / "oidc").write_text("ci-token\n")
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN_FILE", str(tmp_path / "oidc"))
    assert machine.MachineLogin("https://loom.example", "deploy").token() == "t1"
    [form] = token_endpoint
    assert form["grant_type"] == machine.TOKEN_EXCHANGE and form["subject_token"] == "ci-token"
    assert form["client_id"] == "deploy"


def test_a_ci_job_is_asked_for_its_token_with_the_audience(token_endpoint, monkeypatch) -> None:
    asked: dict[str, Any] = {}

    def get(url: str, *, params: dict[str, str], headers: dict[str, str], timeout: float) -> Any:
        asked.update(url=url, params=params, auth=headers["Authorization"])
        return httpx.Response(200, json={"value": "ci-token"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(machine.httpx, "get", get)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://ci.example/token?v=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "job-bearer")
    machine.MachineLogin("https://loom.example", "deploy", audience="loom").token()
    assert asked["params"] == {"audience": "loom"} and asked["auth"] == "Bearer job-bearer"
    assert token_endpoint[0]["subject_token"] == "ci-token"


def test_no_key_and_no_ci_token_says_what_is_missing(token_endpoint) -> None:
    with pytest.raises(AuthInvalid, match="LOOM_CLIENT_KEY_FILE"):
        machine.MachineLogin("https://loom.example", "deploy").token()


def test_the_token_is_kept_until_shortly_before_it_runs_out(
    token_endpoint, tmp_path, monkeypatch
) -> None:
    (tmp_path / "oidc").write_text("ci-token")
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN_FILE", str(tmp_path / "oidc"))
    login = machine.MachineLogin("https://loom.example", "deploy")
    assert login.token() == login.token() == "t1"
    login._expires_at = 0  # an hour later
    assert login.token() == "t2"
    assert login.token(refused="t2") == "t3"  # the server said 401 to it


def test_a_bots_token_goes_only_to_its_server(token_endpoint, tmp_path, monkeypatch) -> None:
    (tmp_path / "oidc").write_text("ci-token")
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN_FILE", str(tmp_path / "oidc"))
    login = machine.MachineLogin("https://loom.example", "deploy")
    with pytest.raises(AuthInvalid):
        next(login.sync_auth_flow(httpx.Request("GET", "https://elsewhere.example/v1/x")))
