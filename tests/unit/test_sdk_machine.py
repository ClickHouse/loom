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
    for var in ("LOOM_WORKLOAD_TOKEN", "LOOM_WORKLOAD_TOKEN_FILE", "ACTIONS_ID_TOKEN_REQUEST_URL"):
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

    def get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
        asked.update(url=url, auth=headers["Authorization"])
        return httpx.Response(200, json={"value": "ci-token"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(machine.httpx, "get", get)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://ci.example/token?api-version=2.0")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "job-bearer")
    machine.MachineLogin("https://loom.example", "deploy", audience="loom").token()
    # The runner's own query stays, the audience joins it.
    assert httpx.URL(asked["url"]).params == httpx.QueryParams("api-version=2.0&audience=loom")
    assert asked["auth"] == "Bearer job-bearer"
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
    login._renew_at = login._expires_at = 0  # an hour later
    assert login.token() == "t2"
    assert login.token(refused="t2") == "t3"  # the server said 401 to it


def test_a_bots_token_goes_only_to_its_server(token_endpoint, tmp_path, monkeypatch) -> None:
    (tmp_path / "oidc").write_text("ci-token")
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN_FILE", str(tmp_path / "oidc"))
    login = machine.MachineLogin("https://loom.example", "deploy")
    with pytest.raises(AuthInvalid):
        next(login.sync_auth_flow(httpx.Request("GET", "https://elsewhere.example/v1/x")))


def test_a_ci_token_given_in_the_environment_is_exchanged(token_endpoint, monkeypatch) -> None:
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "env-token")
    machine.MachineLogin("https://loom.example", "deploy").token()
    assert token_endpoint[0]["subject_token"] == "env-token"


def test_a_renewal_that_fails_keeps_the_token_until_it_runs_out(monkeypatch, tmp_path) -> None:
    # The platform handed the job one CI token: spent on the first exchange.
    spent = {"error": "invalid_grant", "error_description": "used already"}
    answers = [
        httpx.Response(200, json={"access_token": "t1", "expires_in": 3600}),
        httpx.Response(400, json=spent),
        httpx.Response(400, json=spent),
    ]
    monkeypatch.setattr(machine.httpx, "post", lambda *a, **k: answers.pop(0))
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "once")
    login = machine.MachineLogin("https://loom.example", "deploy")
    assert login.token() == "t1"
    login._renew_at, login._expires_at = 0, machine.time.time() + 60  # inside the renewal window
    assert login.token() == "t1"
    login._renew_at = login._expires_at = 0
    with pytest.raises(AuthInvalid):
        login.token()


def test_a_key_signs_for_the_endpoint_the_server_names(monkeypatch, tmp_path) -> None:
    import jwt as pyjwt

    from server.auth import machines

    private, _ = machines.generate_key()
    (tmp_path / "bot.pem").write_bytes(private)
    sent: list[dict[str, str]] = []

    def get(url: str, timeout: float) -> httpx.Response:
        assert url == "http://loom.svc:7777/.well-known/oauth-authorization-server"
        meta = {"token_endpoint": "https://loom.example.com/oauth/token"}
        return httpx.Response(200, json=meta, request=httpx.Request("GET", url))

    def post(url: str, *, data: dict[str, str], timeout: float) -> httpx.Response:
        assert url == "http://loom.svc:7777/oauth/token"  # sent where the bot reaches it
        sent.append(data)
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})

    monkeypatch.setattr(machine.httpx, "get", get)
    monkeypatch.setattr(machine.httpx, "post", post)
    login = machine.MachineLogin(
        "http://loom.svc:7777", "triage", key_file=str(tmp_path / "bot.pem")
    )
    login.token()
    claims = pyjwt.decode(sent[0]["client_assertion"], options={"verify_signature": False})
    assert claims["aud"] == "https://loom.example.com/oauth/token"


def test_a_short_token_is_renewed_halfway(monkeypatch) -> None:
    answers = [
        httpx.Response(200, json={"access_token": f"t{i}", "expires_in": 240}) for i in (1, 2)
    ]
    monkeypatch.setattr(machine.httpx, "post", lambda *a, **k: answers.pop(0))
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    login = machine.MachineLogin("https://loom.example", "deploy")
    assert login.token() == "t1"
    assert login._expires_at - login._renew_at == pytest.approx(120, abs=2)


def test_the_cli_reads_a_bots_claims_before_its_first_request(token_endpoint, monkeypatch) -> None:
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    login = machine.MachineLogin("https://loom.example", "deploy")
    assert login.current_token() == "t1" and len(token_endpoint) == 1


def test_a_renewal_lost_to_the_network_keeps_the_token_and_waits(monkeypatch) -> None:
    calls: list[int] = []

    def post(*a: Any, **k: Any) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"access_token": "t1", "expires_in": 3600})
        raise httpx.ConnectError("Loom unreachable")

    monkeypatch.setattr(machine.httpx, "post", post)
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    login = machine.MachineLogin("https://loom.example", "deploy")
    login.token()
    login._renew_at = 0  # due for renewal, with the token still good
    assert [login.token() for _ in range(5)] == ["t1"] * 5
    assert len(calls) == 2  # one failed renewal, then it waits


def test_a_page_that_is_not_loom_keeps_the_token_in_hand(monkeypatch) -> None:
    answers = [
        httpx.Response(200, json={"access_token": "t1", "expires_in": 3600}),
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(502, text="{not json", headers={"content-type": "application/json"}),
    ]
    monkeypatch.setattr(machine.httpx, "post", lambda *a, **k: answers.pop(0))
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    login = machine.MachineLogin("https://loom.example", "deploy")
    login.token()
    for _ in range(2):
        login._renew_at = 0
        assert login.token() == "t1"


@pytest.mark.parametrize(("status", "category"), [(503, "dependency"), (429, "capacity")])
def test_a_busy_server_is_retryable_not_a_refusal(monkeypatch, status: int, category: str) -> None:
    from loom.errors import BackendUnavailable

    monkeypatch.setattr(machine.httpx, "post", lambda *a, **k: httpx.Response(status, json={}))
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    with pytest.raises(BackendUnavailable) as info:
        machine.MachineLogin("https://loom.example", "deploy").token()
    assert (info.value.category, info.value.retryable) == (category, True)


_FAILED = [
    ("busy", "BackendUnavailable"),
    ("refused", "AuthInvalid"),
    ("not json", "AuthInvalid"),
    ("no key", "AuthInvalid"),
    ("unreachable", "BackendUnavailable"),
]


def _answer(url: str, which: str) -> httpx.Response:
    request = httpx.Request("GET", url)
    if which == "unreachable":
        raise httpx.ConnectError("refused", request=request)
    return {
        "busy": httpx.Response(503, request=request),
        "refused": httpx.Response(403, request=request),
        "not json": httpx.Response(200, text="<html>sign in</html>", request=request),
        "no key": httpx.Response(200, json={"other": 1}, request=request),
    }[which]


@pytest.mark.parametrize(("which", "error"), _FAILED)
def test_a_failed_discovery_is_a_loom_error(monkeypatch, which: str, error: str) -> None:
    monkeypatch.setattr(machine.httpx, "get", lambda url, timeout: _answer(url, which))
    login = machine.MachineLogin(
        "https://loom.example", "deploy", key_source=("LOOM_CLIENT_KEY", "k")
    )
    with pytest.raises(getattr(machine, error)):
        login.token()


@pytest.mark.parametrize(("which", "error"), _FAILED)
def test_a_failed_ci_proof_fetch_is_a_loom_error(
    token_endpoint, monkeypatch, which: str, error: str
) -> None:
    def get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        return _answer(url, which)

    monkeypatch.setattr(machine.httpx, "get", get)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://ci.example/token")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "job-bearer")
    with pytest.raises(getattr(machine, error)):
        machine.MachineLogin("https://loom.example", "deploy", audience="loom").token()
    assert token_endpoint == []


def test_a_malformed_answer_keeps_the_token_in_hand(monkeypatch) -> None:
    answers = [
        httpx.Response(200, json={"access_token": "t1", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "t2", "expires_in": None}),
    ]
    monkeypatch.setattr(machine.httpx, "post", lambda *a, **k: answers.pop(0))
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    login = machine.MachineLogin("https://loom.example", "deploy")
    login.token()
    login._renew_at = 0
    assert login.token() == "t1"


@pytest.mark.parametrize(
    "body",
    [
        {"access_token": "a", "expires_in": "soon"},
        {"access_token": "a", "expires_in": None},
        {"access_token": "a", "expires_in": True},
        {"access_token": None},
        {"access_token": ""},
        {"expires_in": 3600},
    ],
)
def test_a_token_answer_of_the_wrong_shape_is_refused(monkeypatch, body: dict[str, Any]) -> None:
    monkeypatch.setattr(machine.httpx, "post", lambda *a, **k: httpx.Response(200, json=body))
    monkeypatch.setenv("LOOM_WORKLOAD_TOKEN", "ci")
    with pytest.raises(AuthInvalid, match="bot 'deploy': "):
        machine.MachineLogin("https://loom.example", "deploy").token()


@pytest.fixture
def signed(monkeypatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    def get(url: str, timeout: float) -> httpx.Response:
        meta = {"token_endpoint": "https://loom.example/oauth/token"}
        return httpx.Response(200, json=meta, request=httpx.Request("GET", url))

    def post(url: str, *, data: dict[str, str], timeout: float) -> httpx.Response:
        sent.append(data)
        return httpx.Response(200, json={"access_token": f"t{len(sent)}", "expires_in": 3600})

    monkeypatch.setattr(machine.httpx, "get", get)
    monkeypatch.setattr(machine.httpx, "post", post)
    for var in machine._KEY_SOURCES:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOOM_CLIENT_ID", "triage")
    return sent


def _verified(form: dict[str, str], public: bytes) -> dict[str, Any]:
    import jwt as pyjwt

    aud = "https://loom.example/oauth/token"
    return dict(pyjwt.decode(form["client_assertion"], public, algorithms=["ES256"], audience=aud))


def test_a_key_given_in_the_environment_signs(signed, monkeypatch) -> None:
    # As a secret manager injects it (an ECS or Kubernetes secret as an env var).
    from server.auth import machines

    private, public = machines.generate_key()
    monkeypatch.setenv("LOOM_CLIENT_KEY", private.decode())
    machine.machine_login("https://loom.example").token()  # type: ignore[union-attr]
    assert _verified(signed[0], public)["sub"] == "triage"


def test_a_key_in_aws_secrets_manager_is_read_for_each_token(signed, monkeypatch) -> None:
    import types

    from server.auth import machines

    keys = [machines.generate_key(), machines.generate_key()]
    asked: list[tuple[str | None, str]] = []

    class _Client:
        def __init__(self, region: str | None) -> None:
            self.region = region

        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            asked.append((self.region, SecretId))
            return {"SecretString": keys[len(asked) - 1][0].decode()}  # rotated in between

    fake = types.SimpleNamespace(client=lambda name, region_name=None: _Client(region_name))
    monkeypatch.setitem(sys.modules, "boto3", fake)
    arn = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:loom/triage"
    monkeypatch.setenv("LOOM_CLIENT_KEY_SECRET", arn)
    login = machine.machine_login("https://loom.example")
    assert login is not None
    login.token()
    login.token(refused="t1")  # the next token signs with the rotated key
    assert asked == [("eu-west-1", arn)] * 2
    assert _verified(signed[0], keys[0][1]) and _verified(signed[1], keys[1][1])


def test_secrets_manager_away_keeps_the_token_in_hand(signed, monkeypatch) -> None:
    import types

    from server.auth import machines

    calls = {"n": 0}

    class _Client:
        def get_secret_value(self, SecretId: str) -> dict[str, str]:
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("throttled")
            return {"SecretString": machines.generate_key()[0].decode()}

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _Client())
    )
    monkeypatch.setenv("LOOM_CLIENT_KEY_SECRET", "loom/triage")
    login = machine.machine_login("https://loom.example")
    assert login is not None and login.token() == "t1"
    login._renew_at = 0  # due for renewal, and the secret cannot be read now
    assert login.token() == "t1"


def test_two_key_sources_at_once_are_refused(signed, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOOM_CLIENT_KEY_FILE", str(tmp_path / "bot.pem"))
    monkeypatch.setenv("LOOM_CLIENT_KEY", "example-key")
    with pytest.raises(AuthInvalid, match="not several"):
        machine.machine_login("https://loom.example")


def test_a_key_that_is_not_a_pem_names_its_source(signed, monkeypatch) -> None:
    # As `docker --env-file` leaves it: the newlines written as "\n".
    from server.auth import machines

    private, _ = machines.generate_key()
    monkeypatch.setenv("LOOM_CLIENT_KEY", private.decode().replace("\n", "\\n"))
    with pytest.raises(AuthInvalid, match="LOOM_CLIENT_KEY: not a PEM private key"):
        machine.machine_login("https://loom.example").token()  # type: ignore[union-attr]


def test_a_secret_stored_as_binary_is_a_configuration_error(signed, monkeypatch) -> None:
    import types

    class _Client:
        def get_secret_value(self, SecretId: str) -> dict[str, bytes]:
            return {"SecretBinary": b"\x01"}

    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda *a, **k: _Client())
    )
    monkeypatch.setenv("LOOM_CLIENT_KEY_SECRET", "loom/triage")
    with pytest.raises(AuthInvalid, match="stored as text"):
        machine.machine_login("https://loom.example").token()  # type: ignore[union-attr]
