"""The SDK's stored sign-in (`loom login`): the credentials file, refresh and
its lock, and which credential an Agent picks. The server is a MockTransport."""

from __future__ import annotations

import json
import stat
import time
from typing import Any

import httpx
import pytest
from loom import credentials
from loom.errors import AuthInvalid

_SERVER = "https://loom.example"
_META = {
    "device_authorization_endpoint": f"{_SERVER}/oauth/device_authorization",
    "token_endpoint": f"{_SERVER}/oauth/token",
    "revocation_endpoint": f"{_SERVER}/oauth/revoke",
}


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", tmp_path / "loom" / "credentials")
    monkeypatch.delenv("LOOM_PROFILE", raising=False)
    monkeypatch.delenv("LOOM_BASE_URL", raising=False)
    monkeypatch.delenv("LOOM_API_KEY", raising=False)
    return tmp_path


class _Server:
    """A fake Loom: answers discovery, the device grant (pending once, then
    approved) and refresh; records what it was sent."""

    def __init__(self) -> None:
        # path -> responses to give before answering normally
        self.queued: dict[str, list[httpx.Response]] = {}
        self.sent: list[tuple[str, dict[str, str]]] = []
        self.pending = 1
        self.refresh_ok = True
        self.n = 0

    def tokens(self) -> dict[str, Any]:
        self.n += 1
        return {"access_token": f"a{self.n}", "refresh_token": f"r{self.n}", "expires_in": 3600}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        self.sent.append((request.url.path, form))
        path = request.url.path
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json=_META)
        if path == "/oauth/device_authorization":
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "BCDF-GHJK",
                    "verification_uri": f"{_SERVER}/oauth/device",
                    "verification_uri_complete": f"{_SERVER}/oauth/device?user_code=BCDF-GHJK",
                    "expires_in": 600,
                    "interval": 0,
                },
            )
        if path == "/oauth/token" and form["grant_type"].endswith("device_code"):
            if self.pending:
                self.pending -= 1
                return httpx.Response(400, json={"detail": {"error": "authorization_pending"}})
            return httpx.Response(200, json=self.tokens())
        if path == "/oauth/token":
            if not self.refresh_ok:
                return httpx.Response(400, json={"detail": {"error": "invalid_grant"}})
            return httpx.Response(200, json=self.tokens())
        if path == "/oauth/revoke":
            return httpx.Response(200)
        if path == "/v1/whoami":
            auth = request.headers.get("authorization", "")
            return httpx.Response(
                200, json={"authenticated": True, "subject": "alice", "org": "acme", "bearer": auth}
            )
        return httpx.Response(404)


@pytest.fixture
def server(monkeypatch):
    fake = _Server()
    real = httpx.Client

    def client(*args: Any, **kw: Any) -> httpx.Client:
        kw.setdefault("transport", httpx.MockTransport(fake))
        return real(*args, **kw)

    monkeypatch.setattr(credentials.httpx, "Client", client)
    return fake


def _stored() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(credentials.CREDENTIALS_FILE.read_text())
    return data


def test_login_stores_the_session_readable_by_this_user_only(server) -> None:
    printed: list[str] = []
    info = credentials.login(_SERVER, open_browser=False, echo=printed.append)
    assert "BCDF-GHJK" in printed[0]
    assert info["subject"] == "alice"
    entry = _stored()["profiles"]["default"]
    assert (entry["server"], entry["access_token"], entry["refresh_token"]) == (_SERVER, "a1", "r1")
    mode = stat.S_IMODE(credentials.CREDENTIALS_FILE.stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(credentials.CREDENTIALS_FILE.parent.stat().st_mode) == 0o700


def test_login_backs_off_on_slow_down(server, monkeypatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(credentials.time, "sleep", waits.append)
    server.pending = 0
    server.queued["/oauth/token"] = [httpx.Response(400, json={"detail": {"error": "slow_down"}})]
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert waits == [0, 5]


def test_a_denied_login_raises(server) -> None:
    server.queued["/oauth/token"] = [
        httpx.Response(400, json={"detail": {"error": "access_denied"}})
    ]
    server.pending = 0
    with pytest.raises(AuthInvalid, match="denied"):
        credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert not credentials.CREDENTIALS_FILE.exists()


def test_profiles_keep_several_servers_apart(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    credentials.login(_SERVER, profile="staging", open_browser=False, echo=lambda _: None)
    assert set(_stored()["profiles"]) == {"default", "staging"}


def test_an_expiring_token_is_refreshed_and_rotated(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    data = _stored()
    data["profiles"]["default"]["expires_at"] = int(time.time()) + 10
    credentials.CREDENTIALS_FILE.write_text(json.dumps(data))
    assert credentials.access_token() == "a2"
    assert _stored()["profiles"]["default"]["refresh_token"] == "r2"
    assert server.sent[-1][1]["refresh_token"] == "r1"


def test_a_token_another_process_already_rotated_is_not_refreshed_again(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    refused = credentials.access_token()
    # Another process refreshed while this one waited for the lock.
    data = _stored()
    data["profiles"]["default"]["access_token"] = "fresh"
    credentials.CREDENTIALS_FILE.write_text(json.dumps(data))
    sent = len(server.sent)
    assert credentials.access_token(refused=refused) == "fresh"
    assert len(server.sent) == sent


def test_an_ended_session_is_forgotten(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.refresh_ok = False
    with pytest.raises(AuthInvalid, match="run `loom login`"):
        credentials.access_token(refused="a1")
    assert "default" not in _stored()["profiles"]


def test_logout_revokes_the_refresh_token_and_forgets_it(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert credentials.logout() is True
    assert server.sent[-1] == ("/oauth/revoke", {"token": "r1", "client_id": "loom-cli"})
    assert _stored()["profiles"] == {}


def test_a_profile_is_never_sent_to_another_server(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert credentials.stored_login("https://other.example") is None
    assert credentials.stored_login("http://localhost:7777") is None
    assert credentials.stored_login(_SERVER + "/") is not None


def test_an_agent_uses_the_stored_login_and_an_api_key_wins(server, monkeypatch) -> None:
    from loom import Agent

    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    # No server named: localhost, and the session (for another server) stays home.
    assert "login:" not in repr(Agent("acme", "alice", "notebook").transport)
    monkeypatch.setenv("LOOM_BASE_URL", _SERVER)
    agent = Agent("acme", "alice", "notebook")
    assert agent.transport.base_url == _SERVER
    assert "login:default" in repr(agent.transport)
    monkeypatch.setenv("LOOM_API_KEY", "explicit")
    assert "login:" not in repr(Agent("acme", "alice", "notebook").transport)


def test_the_stored_login_refreshes_once_after_a_401(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.queued["/v1/ping"] = [httpx.Response(401), httpx.Response(200)]
    with httpx.Client(base_url=_SERVER) as c:
        assert (
            c.get("/v1/ping", auth=credentials.StoredLogin("default", _SERVER)).status_code == 200
        )
    # The 401 led to one refresh (r1 spent) and the retry carried the new token.
    assert [form.get("refresh_token") for path, form in server.sent if path == "/oauth/token"][
        -1
    ] == "r1"
    assert _stored()["profiles"]["default"]["access_token"] == "a2"


def test_sign_in_endpoints_on_another_host_are_refused(server) -> None:
    server.queued["/.well-known/oauth-authorization-server"] = [
        httpx.Response(
            200, json={**_META, "token_endpoint": "https://elsewhere.example/oauth/token"}
        )
    ]
    with pytest.raises(credentials.LoomError, match="elsewhere.example"):
        credentials.login(_SERVER, open_browser=False, echo=lambda _: None)


def test_the_code_line_is_flushed_for_a_piped_terminal(monkeypatch) -> None:
    flushes: list[bool] = []
    monkeypatch.setattr("builtins.print", lambda *a, flush=False, **kw: flushes.append(flush))
    credentials._say("To sign in, open …")
    assert flushes == [True]


def test_login_keeps_polling_through_a_server_hiccup(server, monkeypatch) -> None:
    monkeypatch.setattr(credentials.time, "sleep", lambda _: None)
    server.queued["/oauth/token"] = [httpx.Response(502), httpx.Response(503)]
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert _stored()["profiles"]["default"]["access_token"]


def test_a_refresh_the_server_cannot_answer_now_is_retryable(server) -> None:
    from loom.errors import BackendUnavailable

    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.queued["/oauth/token"] = [
        httpx.Response(503, json={"detail": {"error": "temporarily_unavailable"}})
    ]
    with pytest.raises(BackendUnavailable):
        credentials.access_token(refused="a1")
    assert _stored()["profiles"]["default"]["refresh_token"] == "r1"  # kept for the retry


def test_a_failed_sign_in_request_is_a_typed_sdk_error(server) -> None:
    from loom.errors import BackendUnavailable

    server.queued["/oauth/device_authorization"] = [httpx.Response(502)]
    with pytest.raises(BackendUnavailable):
        credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    # A poll answered invalid_grant (the code spent by a poll whose answer was lost).
    server.pending = 0
    server.queued["/oauth/token"] = [
        httpx.Response(400, json={"detail": {"error": "invalid_grant"}})
    ]
    with pytest.raises(AuthInvalid, match="loom login"):
        credentials.login(_SERVER, open_browser=False, echo=lambda _: None)


def test_an_agent_can_name_its_profile(server, monkeypatch) -> None:
    from loom import Agent

    credentials.login(_SERVER, profile="work", open_browser=False, echo=lambda _: None)
    assert "login:" not in repr(Agent("acme", "a", "n", base_url=_SERVER).transport)
    assert "login:work" in repr(Agent("acme", "a", "n", base_url=_SERVER, profile="work").transport)


def test_an_unreachable_server_is_a_typed_sdk_error(monkeypatch) -> None:
    from loom.errors import BackendUnavailable

    with pytest.raises(BackendUnavailable, match="cannot reach"):
        credentials.login("http://127.0.0.1:1", open_browser=False, echo=lambda _: None)


def test_an_explicit_profile_wins_and_keeps_to_its_server(server, monkeypatch) -> None:
    from loom import Agent

    credentials.login(_SERVER, profile="work", open_browser=False, echo=lambda _: None)
    monkeypatch.setenv("LOOM_API_KEY", "from-the-environment")
    monkeypatch.setenv("LOOM_BASE_URL", "https://elsewhere.example")
    agent = Agent("acme", "a", "n", profile="work")
    assert agent.transport.base_url == _SERVER and "login:work" in repr(agent.transport)
    with pytest.raises(ValueError, match="work"):
        Agent("acme", "a", "n", profile="work", base_url="https://elsewhere.example")
    with pytest.raises(AuthInvalid):
        Agent("acme", "a", "n", profile="never-signed-in")


def test_a_refresh_that_never_reached_the_server_is_tried_again(server, monkeypatch) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    real = server.__class__.__call__
    refused = {"n": 0}

    def no_connection_once(self: Any, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token" and refused["n"] == 0:
            refused["n"] += 1
            raise httpx.ConnectError("connection refused", request=request)
        return real(self, request)

    monkeypatch.setattr(server.__class__, "__call__", no_connection_once)
    assert credentials.access_token(refused="a1") == "a2"


def test_a_refresh_whose_answer_is_lost_is_not_resent(server, monkeypatch) -> None:
    """The server may have spent the token: presenting it again would end the
    session, so the SDK reports a retryable failure instead."""
    from loom.errors import BackendUnavailable

    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    real = server.__class__.__call__
    sent: list[str] = []

    def answer_lost(self: Any, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            sent.append(dict(httpx.QueryParams(request.content.decode()))["refresh_token"])
            real(self, request)  # the server rotates the token...
            raise httpx.ReadTimeout("answer lost", request=request)  # ...and the answer is lost
        return real(self, request)

    monkeypatch.setattr(server.__class__, "__call__", answer_lost)
    with pytest.raises(BackendUnavailable):
        credentials.access_token(refused="a1")
    assert sent == ["r1"]  # once, never resent


def test_login_says_who_it_signed_in(server) -> None:
    printed: list[str] = []
    credentials.login(_SERVER, open_browser=False, echo=printed.append)
    assert printed[-1] == f"Signed in to {_SERVER} as alice (acme)."


def test_an_early_refresh_that_fails_keeps_the_token_that_still_works(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    data = _stored()
    data["profiles"]["default"]["expires_at"] = int(time.time()) + 30  # inside the margin
    credentials.CREDENTIALS_FILE.write_text(json.dumps(data))
    server.queued["/oauth/token"] = [
        httpx.Response(503, json={"detail": {"error": "temporarily_unavailable"}})
    ]
    assert credentials.access_token() == "a1"


def test_a_client_never_sends_a_token_from_another_server(server) -> None:
    """A profile signed in again to another server must not lend that server's
    token to an Agent made for the first one."""
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    auth = credentials.stored_login(_SERVER)
    assert auth is not None
    data = _stored()
    data["profiles"]["default"] |= {
        "server": "https://other.example",
        "access_token": "other-token",
    }
    credentials.CREDENTIALS_FILE.write_text(json.dumps(data))
    assert auth.current_token() == ""
    with httpx.Client(base_url=_SERVER) as c, pytest.raises(AuthInvalid, match="now signed in"):
        c.get("/v1/ping", auth=auth)


def test_a_stored_session_is_only_sent_to_its_own_server(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    auth = credentials.StoredLogin("default", _SERVER)
    with httpx.Client() as c, pytest.raises(AuthInvalid, match="refusing to send"):
        c.get("https://elsewhere.example/v1/ping", auth=auth)
