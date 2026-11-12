"""The SDK's stored sign-in (`loom login`): the credentials file, refresh and
its lock, and which credential an Agent picks. The server is a MockTransport."""

from __future__ import annotations

import errno
import json
import os
import stat
import time
from typing import Any

import httpx
import pytest
from loom import cli, credentials
from loom.errors import AuthInvalid

from tests.unit.clock_fake import ManualClock

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
        self.while_revoking: Any = None  # run while a revoke is in flight
        self.marks = True  # False: a proxy in front strips the Loom-Sessions header

    def tokens(self) -> dict[str, Any]:
        self.n += 1
        return {"access_token": f"a{self.n}", "refresh_token": f"r{self.n}", "expires_in": 3600}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        r = self.answer(request)
        marks = ("/oauth/token", "/oauth/device_authorization")
        if self.marks and request.url.path in marks and not r.extensions.get("previous_build"):
            r.headers["Loom-Sessions"] = "1"  # as this build signs its answers
        return r

    def answer(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        self.sent.append((request.url.path, form))
        path = request.url.path
        if self.queued.get(path):
            return self.queued[path].pop(0)
        if path == "/.well-known/oauth-authorization-server":
            # Each host advertises its own endpoints, as a cell does at its own host.
            base = f"{request.url.scheme}://{request.url.host}"
            return httpx.Response(200, json={k: v.replace(_SERVER, base) for k, v in _META.items()})
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
            if self.while_revoking:
                self.while_revoking()
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


def _previous_build(status: int, **kw: Any) -> httpx.Response:
    """An answer from a build that predates sessions, mid rolling upgrade."""
    return httpx.Response(status, extensions={"previous_build": True}, **kw)


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


@pytest.mark.parametrize(("status", "category"), [(503, "dependency"), (429, "capacity")])
def test_a_refresh_the_server_cannot_answer_now_is_retryable(
    server, status: int, category: str
) -> None:
    from loom.errors import BackendUnavailable

    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.queued["/oauth/token"] = [
        httpx.Response(status, json={"detail": {"error": "temporarily_unavailable"}})
    ]
    with pytest.raises(BackendUnavailable) as info:
        credentials.access_token(refused="a1")
    assert (info.value.category, info.value.retryable) == (category, True)
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
    spelled = _SERVER.replace("loom.example", "LOOM.example") + ":443"
    assert "login:work" in repr(Agent("acme", "a", "n", base_url=spelled, profile="work").transport)


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


def test_a_server_spelled_another_way_is_the_same_server(server) -> None:
    spelled = "https://LOOM.example:443"
    credentials.login(spelled, open_browser=False, echo=lambda _: None)
    auth = credentials.stored_login(_SERVER)
    assert auth is not None
    with httpx.Client(transport=httpx.MockTransport(server)) as c:
        assert c.get(f"{_SERVER}/v1/whoami", auth=auth).status_code == 200
    assert credentials.stored_login("https://loom.example:8443") is None


def test_logout_leaves_a_login_made_while_it_ran(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)

    def another_login() -> None:  # another terminal signs in meanwhile
        tokens = {"access_token": "new", "refresh_token": "rnew", "expires_in": 3600}
        credentials._save("default", _SERVER, tokens, session="another")

    server.while_revoking = another_login
    assert credentials.logout() is True
    assert _stored()["profiles"]["default"]["access_token"] == "new"


def test_logout_forgets_its_session_after_a_refresh_rotated_it(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    session = _stored()["profiles"]["default"]["session"]
    server.while_revoking = lambda: credentials._save(
        "default", _SERVER, {"access_token": "a9", "refresh_token": "r9"}, session=session
    )
    assert credentials.logout() is True
    assert _stored()["profiles"] == {}


def test_an_internationalized_server_is_its_own_server(server) -> None:
    tokens = {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600}
    credentials._save("default", "https://bücher.example", tokens, session="s")
    auth = credentials.stored_login("https://xn--bcher-kva.example")  # as httpx spells it
    assert auth is not None
    with httpx.Client(transport=httpx.MockTransport(server)) as c:
        assert c.get("https://xn--bcher-kva.example/v1/whoami", auth=auth).status_code == 200


def test_two_threads_of_one_process_take_the_credentials_lock_in_turn(
    tmp_path, monkeypatch
) -> None:
    # Async requests refresh on worker threads: two must never spend one token.
    import threading
    import time as _time

    monkeypatch.setattr(credentials, "CREDENTIALS_FILE", tmp_path / "credentials")
    # As on a platform with no file lock: the in-process lock alone must hold.
    monkeypatch.setattr(credentials, "_lock_file", lambda fd: None)
    monkeypatch.setattr(credentials, "_unlock_file", lambda fd: None)
    inside: list[str] = []

    def hold(name: str) -> None:
        with credentials._locked():
            inside.append(f"{name} in")
            _time.sleep(0.1)
            inside.append(f"{name} out")

    threads = [threading.Thread(target=hold, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inside in (["a in", "a out", "b in", "b out"], ["b in", "b out", "a in", "a out"])


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is POSIX")
def test_a_child_forked_mid_refresh_can_still_refresh() -> None:
    with credentials._in_process:  # a refresh running in the parent
        pid = os.fork()
        if pid == 0:
            os._exit(0 if credentials._in_process.acquire(timeout=1) else 1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0


def test_signing_in_elsewhere_ends_the_session_it_replaces(server, monkeypatch) -> None:
    ended: list[tuple[str, str]] = []

    def end(entry: dict[str, Any]) -> None:  # the new session is saved by then
        ended.append((entry["server"], credentials._read()["profiles"]["default"]["server"]))

    monkeypatch.setattr(credentials, "_end_session", end)
    old = {"access_token": "old", "refresh_token": "rold", "expires_in": 3600}
    credentials._save("default", "https://old.example", old, session="s0")
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert ended == [("https://old.example", _SERVER)]  # not left running there for 30 days


@pytest.mark.parametrize(
    "answer",
    [
        _previous_build(400, json={"detail": {"error": "invalid_grant"}}),  # sign-in, no sessions
        _previous_build(404),  # no sign-in at all
    ],
)
def test_a_previous_build_s_refusal_keeps_the_session(server, answer: httpx.Response) -> None:
    from loom.errors import BackendUnavailable

    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    refused = credentials._read()["profiles"]["default"]["access_token"]
    for _ in range(2):  # however often the upgrade routes it there
        server.queued["/oauth/token"] = [answer]
        with pytest.raises(BackendUnavailable, match="predates"):
            credentials.access_token(refused=refused)
    assert credentials.access_token(refused=refused) == "a2"  # this build's answer


@pytest.mark.parametrize("marks", [True, False])  # a header-stripping proxy or not
def test_a_previous_build_without_sign_in_keeps_the_session(server, marks: bool) -> None:
    from loom.errors import BackendUnavailable

    server.marks = marks
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.queued["/.well-known/oauth-authorization-server"] = [
        httpx.Response(404),  # no sign-in
        httpx.Response(200, json={"token_endpoint": f"{_SERVER}/oauth/token"}),  # MCP only
    ]
    for _ in range(2):
        with pytest.raises(BackendUnavailable, match="without `loom login`"):
            credentials.access_token(refused="a1")
    assert credentials.access_token(refused="a1") == "a2"


def test_login_keeps_polling_through_a_previous_build_s_answers(server, monkeypatch) -> None:
    monkeypatch.setattr(credentials.time, "sleep", lambda _: None)
    server.queued["/oauth/token"] = [
        _previous_build(404),
        _previous_build(400, json={"detail": {"error": "unsupported_grant_type"}}),
    ]
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    assert _stored()["profiles"]["default"]["access_token"]


def test_behind_a_proxy_that_strips_the_mark_a_refusal_is_taken_as_it_is(server) -> None:
    server.marks = False
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.refresh_ok = False
    with pytest.raises(AuthInvalid, match="run `loom login`"):
        credentials.access_token(refused=credentials._read()["profiles"]["default"]["access_token"])
    assert credentials.stored_login(_SERVER) is None
    server.queued["/oauth/token"] = [httpx.Response(400, json={"error": "access_denied"})]
    with pytest.raises(AuthInvalid, match="denied"):
        credentials.login(_SERVER, open_browser=False, echo=lambda _: None)


def test_a_session_that_did_end_is_still_given_up(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.refresh_ok = False
    with pytest.raises(AuthInvalid, match="run `loom login`"):
        credentials.access_token(refused=credentials._read()["profiles"]["default"]["access_token"])
    assert credentials.stored_login(_SERVER) is None


def test_a_replay_s_warning_is_kept(server) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    replayed = "this session was ended because a spent refresh token was presented again"
    server.queued["/oauth/token"] = [
        httpx.Response(400, json={"error": "invalid_grant", "error_description": replayed}),
    ]
    with pytest.raises(AuthInvalid, match="service_accounts"):
        credentials.access_token(refused=credentials._read()["profiles"]["default"]["access_token"])


def test_a_refresh_answered_without_the_mark_still_rotates(server) -> None:
    # The server spent the old token: its answer is kept whatever its headers.
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    server.marks = False
    assert credentials.access_token(refused="a1") == "a2"
    # Still a server that keeps sessions: a previous build's refusal next keeps it.
    from loom.errors import BackendUnavailable

    server.queued["/oauth/token"] = [_previous_build(400, json={"error": "invalid_grant"})]
    with pytest.raises(BackendUnavailable, match="predates"):
        credentials.access_token(refused="a2")
    assert credentials.stored_login(_SERVER) is not None


def _lock_dir(home) -> Any:
    return home / "loom" / ".credentials.lock.d"


def test_the_credentials_lock_holds_a_lock_directory_naming_its_holder(_home) -> None:
    # The lock the TypeScript SDK takes too: Node has no flock.
    with credentials._locked():
        owner = json.loads((_lock_dir(_home) / "owner").read_text())
        assert owner["pid"] == os.getpid()
        assert isinstance(owner["host"], str) and isinstance(owner["time"], float)
    assert not _lock_dir(_home).exists()


def test_the_credentials_lock_waits_while_another_process_holds_the_directory(_home) -> None:
    import threading
    import time as _time

    lock = _held_by(_home, 1, "another-machine")  # as a Node process mid-refresh holds it
    threading.Timer(0.3, lambda: credentials.shutil.rmtree(lock)).start()
    t0 = _time.monotonic()
    with credentials._locked():
        waited = _time.monotonic() - t0
    assert waited >= 0.25


def _held_by(home: Any, pid: int, host: str, age_s: float = 0.0) -> Any:
    lock = _lock_dir(home)
    lock.mkdir(parents=True)
    (lock / "owner").write_text(json.dumps({"pid": pid, "host": host, "time": time.time()}))
    _age(lock, age_s)
    return lock


def _age(path: Any, age_s: float) -> None:
    at = time.time() - age_s
    os.utime(path, (at, at))


def _leftovers(home: Any) -> list[str]:
    return sorted(p.name for p in (home / "loom").glob(".credentials.lock.d.*"))


def _dead_pid() -> int:
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_a_lock_whose_holder_on_another_machine_went_silent_is_broken(_home) -> None:
    lock = _held_by(_home, 1, "another-machine", credentials._LOCK_STALE_S + 1)
    t0 = time.monotonic()
    with credentials._locked():
        assert json.loads((lock / "owner").read_text())["pid"] == os.getpid()
    assert time.monotonic() - t0 < 2
    assert not lock.exists()
    assert _leftovers(_home) == []  # the renamed lock is removed too


def test_a_lock_that_missed_two_heartbeats_and_whose_holder_here_died_is_broken(_home) -> None:
    import socket

    lock = _held_by(_home, _dead_pid(), socket.gethostname(), credentials._LOCK_SILENT_S + 1)
    t0 = time.monotonic()
    with credentials._locked():
        assert json.loads((lock / "owner").read_text())["pid"] == os.getpid()
    assert time.monotonic() - t0 < 2


@pytest.mark.parametrize(
    ("where", "alive"),
    [("this machine", True), ("another machine", False), ("this machine", False)],
)
def test_a_lock_with_a_recent_heartbeat_is_waited_for(_home, where, alive) -> None:
    import socket
    import threading

    # pid 1 runs (as another user: EPERM counts as running); a holder on
    # another machine cannot be asked, and one in another pid namespace here
    # (a container sharing the home) looks dead: its heartbeat decides.
    host = socket.gethostname() if where == "this machine" else "another-machine"
    lock = _held_by(_home, 1 if alive else _dead_pid(), host)
    threading.Timer(0.3, lambda: credentials.shutil.rmtree(lock)).start()
    t0 = time.monotonic()
    with credentials._locked():
        waited = time.monotonic() - t0
    assert waited >= 0.25


def test_the_holder_touches_the_lock_while_it_holds_it(_home, monkeypatch) -> None:
    monkeypatch.setattr(credentials, "_LOCK_HEARTBEAT_S", 0.05)
    lock = _lock_dir(_home)
    with credentials._locked():
        old = time.time() - 3600
        os.utime(lock, (old, old))
        time.sleep(0.3)
        assert time.time() - lock.stat().st_mtime < 5


def test_breaking_leaves_a_lock_that_changed_hands_or_another_waiter_is_breaking(_home) -> None:
    lock = _held_by(_home, 1, "another-machine", credentials._LOCK_STALE_S + 1)
    held = (lock / "owner").read_text()
    claim = lock.with_name(".credentials.lock.d.breaking")
    # Taken over between the read and the break: left alone, claim withdrawn.
    credentials._break(lock, '{"pid": 2}')
    assert lock.exists() and not claim.exists()
    # Another waiter is breaking it: this one waits, and the lock still looks stale.
    claim.mkdir()
    credentials._break(lock, held)
    assert (lock / "owner").read_text() == held
    assert credentials._stale(lock, held)
    claim.rmdir()
    credentials._break(lock, held)
    assert not lock.exists() and _leftovers(_home) == []


def test_a_claim_left_by_a_breaker_that_died_is_cleared_once_old(_home) -> None:
    lock = _held_by(_home, 1, "another-machine", credentials._LOCK_STALE_S + 1)
    claim = lock.with_name(".credentials.lock.d.breaking")
    claim.mkdir()
    _age(claim, credentials._LOCK_STALE_S + 1)
    t0 = time.monotonic()
    with credentials._locked():
        assert json.loads((lock / "owner").read_text())["pid"] == os.getpid()
    assert time.monotonic() - t0 < 2
    # Renamed away, never removed by its path, and deleted by the next sweep.
    (left,) = _leftovers(_home)
    assert left.startswith(".credentials.lock.d.breaking.") and left.endswith(".stale")
    with credentials._locked():
        pass
    assert _leftovers(_home) == []


def test_clearing_an_old_claim_leaves_a_fresh_one_made_after_it(_home) -> None:
    lock = _held_by(_home, 1, "another-machine", credentials._LOCK_STALE_S + 1)
    held = (lock / "owner").read_text()
    claim = lock.with_name(".credentials.lock.d.breaking")
    claim.mkdir()
    _age(claim, credentials._LOCK_STALE_S + 1)
    credentials._break(lock, held)
    assert not claim.exists()
    claim.mkdir()  # another waiter's, made right after
    credentials._break(lock, held)
    assert claim.exists() and (lock / "owner").read_text() == held


def test_a_release_waits_for_a_breaker_s_claim(_home) -> None:
    import threading

    lock = _lock_dir(_home)
    claim = lock.with_name(".credentials.lock.d.breaking")
    with credentials._locked():
        claim.mkdir()  # a waiter breaking it, judged stale
        threading.Timer(0.3, claim.rmdir).start()
        t0 = time.monotonic()
    assert time.monotonic() - t0 >= 0.25
    assert not lock.exists() and not claim.exists()


def test_a_release_that_never_gets_the_claim_leaves_the_lock(_home, monkeypatch) -> None:
    # Renaming it then could rename away a lock a third process takes after it.
    monkeypatch.setattr(credentials, "_RELEASE_WAIT_S", 0.1)
    lock = _lock_dir(_home)
    claim = lock.with_name(".credentials.lock.d.breaking")
    with credentials._locked():
        claim.mkdir()
        owner = (lock / "owner").read_text()
    assert (lock / "owner").read_text() == owner and claim.exists()


def test_a_heartbeat_that_cannot_start_still_releases_the_lock(_home, monkeypatch) -> None:
    def fail(self: Any) -> None:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(credentials.threading.Thread, "start", fail)
    with pytest.raises(RuntimeError):
        with credentials._locked():
            pass
    assert not _lock_dir(_home).exists()


def _busy_rename(monkeypatch: Any, path: Any, times: int, code: int = errno.EACCES) -> list[int]:
    """Refuse the next `times` renames of `path` as Windows does while a file
    in it is open; answers the list of refusals made."""
    refused: list[int] = []
    real = credentials.os.rename

    def rename(src: Any, dst: Any) -> None:
        if str(src) == str(path) and len(refused) < times:
            refused.append(code)
            raise OSError(code, os.strerror(code))
        real(src, dst)

    monkeypatch.setattr(credentials.os, "rename", rename)
    return refused


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.EBUSY])
def test_releasing_retries_a_rename_refused_while_a_waiter_reads_the_owner(
    _home, monkeypatch, code
) -> None:
    lock = _lock_dir(_home)
    with credentials._locked():
        refused = _busy_rename(monkeypatch, lock, 2, code)
    assert refused == [code, code]
    assert not lock.exists() and _leftovers(_home) == []


def test_a_busy_stale_lock_is_broken_on_a_later_try(_home, monkeypatch) -> None:
    lock = _held_by(_home, 1, "another-machine", credentials._LOCK_STALE_S + 1)
    held = (lock / "owner").read_text()
    _busy_rename(monkeypatch, lock, credentials._RENAME_TRIES, errno.EBUSY)
    credentials._break(lock, held)  # busy every try: no error, left for the next
    assert (lock / "owner").read_text() == held
    assert not lock.with_name(".credentials.lock.d.breaking").exists()
    with credentials._locked():
        assert json.loads((lock / "owner").read_text())["pid"] == os.getpid()


def test_a_stale_lock_busy_past_twice_the_stale_age_raises(_home, monkeypatch) -> None:
    lock = _held_by(_home, 1, "another-machine", 2 * credentials._LOCK_STALE_S + 1)
    _busy_rename(monkeypatch, lock, 10**6)
    with pytest.raises(credentials.LoomError, match="is stale but cannot be broken"):
        with credentials._locked():
            pass
    assert lock.exists()


def test_the_lock_waits_as_long_as_the_typescript_sdk_does() -> None:
    assert credentials._RELEASE_WAIT_S == 2.0
    assert credentials._RENAME_TRIES == 5
    assert credentials._RENAME_WAIT_S == 0.02


def _refuse_taking(monkeypatch: Any, lock: Any, times: int, code: int) -> list[int]:
    """Refuse the next `times` renames onto `lock`, as Windows does while a
    scanner holds a file in the private directory open."""
    refused: list[int] = []
    real = credentials.os.rename

    def rename(src: Any, dst: Any) -> None:
        if str(dst) == str(lock) and len(refused) < times:
            refused.append(code)
            raise OSError(code, os.strerror(code))
        real(src, dst)

    monkeypatch.setattr(credentials.os, "rename", rename)
    return refused


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM, errno.EBUSY])
def test_a_lock_that_is_not_there_is_taken_once_refusals_stop(_home, monkeypatch, code) -> None:
    lock = _lock_dir(_home)
    refused = _refuse_taking(monkeypatch, lock, credentials._RENAME_TRIES - 1, code)
    with credentials._locked():
        assert json.loads((lock / "owner").read_text())["pid"] == os.getpid()
    assert len(refused) == credentials._RENAME_TRIES - 1
    assert not lock.exists() and _leftovers(_home) == []


def test_a_lock_that_is_not_there_and_is_refused_every_try_raises(_home, monkeypatch) -> None:
    lock = _lock_dir(_home)
    refused = _refuse_taking(monkeypatch, lock, 10**6, errno.EACCES)
    with pytest.raises(credentials.LoomError, match="cannot take the credentials lock at") as e:
        with credentials._locked():
            pass
    assert str(lock) in str(e.value) and os.strerror(errno.EACCES) in str(e.value)
    assert len(refused) == credentials._RENAME_TRIES
    assert _leftovers(_home) == []
    lock.mkdir()  # there: held, so waited for
    (lock / "owner").write_text("{}")
    assert credentials._create(lock, "{}") is False


def test_a_release_s_claim_is_dated_by_this_machine_s_clock(_home, monkeypatch) -> None:
    # A file server whose clock lags would otherwise make a fresh claim look old to a breaker.
    claim = _lock_dir(_home).with_name(".credentials.lock.d.breaking")
    ages: list[float] = []
    real_mkdir, real_rmdir = credentials.Path.mkdir, credentials.Path.rmdir

    def mkdir(self: Any, *args: Any, **kw: Any) -> None:
        real_mkdir(self, *args, **kw)
        if self == claim:
            _age(claim, 3600)  # dated by a file server an hour behind

    def rmdir(self: Any) -> None:
        if self == claim:
            ages.append(time.time() - claim.stat().st_mtime)
        real_rmdir(self)

    with credentials._locked():
        monkeypatch.setattr(credentials.Path, "mkdir", mkdir)
        monkeypatch.setattr(credentials.Path, "rmdir", rmdir)
    assert len(ages) == 1 and ages[0] < 5


def test_taking_the_lock_sweeps_what_a_dead_taker_or_breaker_left(_home) -> None:
    loom = _home / "loom"
    for name in (
        ".credentials.lock.d.0a.tmp",
        ".credentials.lock.d.0b.stale",
        ".credentials.lock.d.0c.tmp",
    ):
        (loom / name).mkdir(parents=True)
        (loom / name / "owner").write_text("{}")
    _age(loom / ".credentials.lock.d.0a.tmp", credentials._LOCK_STALE_S + 1)
    _age(loom / ".credentials.lock.d.0b.stale", credentials._LOCK_STALE_S + 1)
    with credentials._locked():
        pass
    assert _leftovers(_home) == [
        ".credentials.lock.d.0c.tmp"
    ]  # another process may be taking it now


@pytest.mark.parametrize("fails", ["claiming it", "renaming it away"])
def test_a_stale_lock_that_cannot_be_broken_raises_and_never_spins(
    _home, monkeypatch, fails
) -> None:
    lock = _held_by(_home, 1, "another-machine", credentials._LOCK_STALE_S + 1)
    denied = PermissionError(13, "Permission denied")
    real_mkdir, real_rename = credentials.Path.mkdir, credentials.os.rename

    def mkdir(self: Any, *args: Any, **kw: Any) -> None:
        if fails == "claiming it" and self.name == ".credentials.lock.d.breaking":
            raise denied
        real_mkdir(self, *args, **kw)

    def rename(src: Any, dst: Any) -> None:
        if fails == "renaming it away" and str(src) == str(lock):
            raise OSError(errno.EROFS, "Read-only file system")
        real_rename(src, dst)

    monkeypatch.setattr(credentials.Path, "mkdir", mkdir)
    monkeypatch.setattr(credentials.os, "rename", rename)
    with pytest.raises(credentials.LoomError, match="is stale but cannot be broken"):
        with credentials._locked():
            pass
    assert lock.exists()


def test_the_lock_is_dated_by_this_machine_s_clock(_home, monkeypatch) -> None:
    # utime with no time takes the file server's clock on a network home.
    touched: list[Any] = []
    real = credentials.os.utime

    def utime(path: Any, times: Any = None, **kw: Any) -> None:
        touched.append((str(path), times))
        real(path, times, **kw)

    monkeypatch.setattr(credentials.os, "utime", utime)
    monkeypatch.setattr(credentials, "_LOCK_HEARTBEAT_S", 0.05)
    with credentials._locked():
        time.sleep(0.2)
    assert touched[0][0].endswith(".tmp")  # before the rename onto the lock
    assert touched[1][0] == str(_lock_dir(_home))  # a heartbeat
    assert all(times is not None for _, times in touched)


def test_a_refresh_s_discovery_is_bounded_by_its_deadline(server, monkeypatch) -> None:
    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    monkeypatch.setattr(credentials, "_REFRESH_DEADLINE_S", 5.0)
    timeouts: dict[str, Any] = {}
    real = server.__class__.__call__

    def record(self: Any, request: httpx.Request) -> httpx.Response:
        timeouts.setdefault(request.url.path, request.extensions["timeout"])
        return real(self, request)

    monkeypatch.setattr(server.__class__, "__call__", record)
    credentials.access_token(refused="a1")
    discovery = timeouts["/.well-known/oauth-authorization-server"]
    assert max(discovery.values()) <= 5.0


def test_a_refresh_ends_within_its_deadline(server, monkeypatch) -> None:
    """A connect failure that used up the deadline is not retried: the lock
    is held for a bounded time, and nothing was spent."""
    from loom.errors import BackendUnavailable

    credentials.login(_SERVER, open_browser=False, echo=lambda _: None)
    real = server.__class__.__call__
    clock = {"skew": 0.0}
    real_monotonic = time.monotonic
    monkeypatch.setattr(credentials.time, "monotonic", lambda: real_monotonic() + clock["skew"])
    posts: list[str] = []

    def slow_connect_failure(self: Any, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            posts.append("token")
            clock["skew"] += credentials._REFRESH_DEADLINE_S
            raise httpx.ConnectTimeout("no connection", request=request)
        return real(self, request)

    monkeypatch.setattr(server.__class__, "__call__", slow_connect_failure)
    with pytest.raises(BackendUnavailable, match="took over 90 s"):
        credentials.access_token(refused="a1")
    assert posts == ["token"]


def test_releasing_leaves_a_lock_directory_another_process_took_over(_home) -> None:
    lock = _lock_dir(_home)
    with credentials._locked():
        (lock / "owner").write_text('{"pid": 1}')  # broken as stale and taken meanwhile
    assert lock.exists()


def _lookup(server: Any, *answers: httpx.Response) -> None:
    server.queued["/cells/lookup"] = list(answers)


def _signins(server: Any) -> list[str]:
    return [path for path, _ in server.sent if path == "/oauth/device_authorization"]


def test_login_with_an_org_signs_in_at_the_cell_the_router_names(server) -> None:
    _lookup(
        server, httpx.Response(200, json={"org": "beta", "cell": "eu-2", "host": "eu-2.example"})
    )
    asked: list[str] = []
    real = server.answer

    def _answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cells/lookup":
            asked.append(str(request.url.params))
        return real(request)

    server.answer = _answer
    printed: list[str] = []
    credentials.login(_SERVER, org="beta", open_browser=False, echo=printed.append)
    assert asked == ["org=beta"]
    assert printed[0] == "Org beta is at https://eu-2.example."
    assert _stored()["profiles"]["default"]["server"] == "https://eu-2.example"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404, json={"detail": "Not Found"}),  # a deployment with no router
        httpx.Response(503, json={"error": "the directory holds no cell yet", "code": "NO_CELL"}),
    ],
)
def test_login_with_an_org_keeps_its_server_with_no_router_or_no_cell(server, answer) -> None:
    _lookup(server, answer)
    credentials.login(_SERVER, org="beta", open_browser=False, echo=lambda _: None)
    assert _stored()["profiles"]["default"]["server"] == _SERVER


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(503, json={"error": "the directory could not be read: OSError"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json={"org": "beta", "cell": "eu-2"}),  # no host
    ],
)
def test_login_with_an_org_refuses_any_other_lookup_answer_and_starts_no_sign_in(
    server, answer
) -> None:
    _lookup(server, answer)
    with pytest.raises(credentials.LoomError, match="router"):
        credentials.login(_SERVER, org="beta", open_browser=False, echo=lambda _: None)
    assert _signins(server) == [] and not credentials.CREDENTIALS_FILE.exists()


@pytest.fixture
def waits(monkeypatch) -> list[float]:
    """The lookup's waits, on a clock they advance."""
    clock = ManualClock()
    monkeypatch.setattr(credentials, "_monotonic", clock)
    monkeypatch.setattr(credentials.time, "sleep", clock.sleep)
    return clock.sleeps


def test_login_with_an_org_waits_out_the_routers_rate_limit(server, waits) -> None:
    _lookup(
        server,
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "too many lookups"}),
        httpx.Response(200, json={"org": "beta", "cell": "eu-2", "host": "eu-2.example"}),
    )
    credentials.login(_SERVER, org="beta", open_browser=False, echo=lambda _: None)
    assert waits[0] == 7.0
    assert _stored()["profiles"]["default"]["server"] == "https://eu-2.example"


def test_login_with_an_org_refuses_as_a_rate_refusal_once_the_wait_is_spent(server, waits) -> None:
    """Retry-After 7 within a 20 s --wait: two waits, then the third would pass it."""
    _lookup(server, *[httpx.Response(429, headers={"Retry-After": "7"}) for _ in range(5)])
    with pytest.raises(credentials.RateLimited, match="asks for 7s and 6s of the wait") as refused:
        credentials.login(_SERVER, org="beta", wait_s=20, open_browser=False, echo=lambda _: None)
    assert waits == [7.0, 7.0] and _signins(server) == []
    assert (refused.value.category, refused.value.retryable, refused.value.status) == (
        "capacity",
        True,
        429,
    )


def test_login_with_an_org_reaches_the_cell_over_https_whatever_the_routers_scheme(
    server,
) -> None:
    _lookup(
        server, httpx.Response(200, json={"org": "beta", "cell": "eu-2", "host": "eu-2.example"})
    )
    credentials.login("http://localhost:8080", org="beta", open_browser=False, echo=lambda _: None)
    assert _stored()["profiles"]["default"]["server"] == "https://eu-2.example"


@pytest.mark.parametrize(
    "host",
    [
        {"a": 1},
        "x@evil.example",
        "eu-2.example/p",
        "a b.example",
        "eu-2.example:port",
        "",
        "?q",
        "[eu-2",
        "999.1.1.1",
        "eu-2.example?",
        "eu-2.example#",
    ],
)
def test_login_with_an_org_refuses_a_host_that_is_not_a_bare_name(server, host) -> None:
    _lookup(server, httpx.Response(200, json={"org": "beta", "cell": "eu-2", "host": host}))
    with pytest.raises(credentials.LoomError, match="router .* no usable host") as refused:
        credentials.login(_SERVER, org="beta", open_browser=False, echo=lambda _: None)
    assert _signins(server) == []
    assert (refused.value.category, refused.value.retryable) == ("internal", False)
    assert refused.value.status == 200


def test_a_lookup_refusal_is_classed_for_the_caller(server) -> None:
    """An outage's 503 is a dependency to retry; a 4xx other than 404 is not."""
    _lookup(server, httpx.Response(503, json={"error": "the directory could not be read"}))
    with pytest.raises(credentials.LoomError) as down:
        credentials.org_server(_SERVER, "beta")
    assert (down.value.category, down.value.retryable, down.value.status) == (
        "dependency",
        True,
        503,
    )
    _lookup(server, httpx.Response(400, json={"error": "a lookup names an org"}))
    with pytest.raises(credentials.LoomError, match="router") as bad:
        credentials.org_server(_SERVER, "beta")
    assert bad.value.retryable is False and bad.value.status == 400


def test_a_lookup_answer_that_is_not_json_refuses_naming_the_router(server) -> None:
    _lookup(server, httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(credentials.LoomError, match="router") as refused:
        credentials.org_server(_SERVER, "beta")
    assert (refused.value.category, refused.value.status) == ("internal", 200)


def test_a_lookup_naming_the_given_server_says_nothing(server) -> None:
    _lookup(server, httpx.Response(200, json={"org": "beta", "cell": "c1", "host": "loom.example"}))
    printed: list[str] = []
    credentials.login("https://Loom.example", org="beta", open_browser=False, echo=printed.append)
    assert not any(line.startswith("Org beta is at") for line in printed)


@pytest.mark.parametrize(
    "argv,said",
    [
        (["login", "--wait", "5"], "--wait applies only with --org"),
        (["login", "--wait", "120"], "--wait applies only with --org"),
        (["login", "--org", "", "--wait", "5"], "--org needs an org"),
        (["login", "--org", "beta", "--wait", "-5"], "0 or more"),
        (["login", "--org", "beta", "--wait", "nan"], "0 or more"),
        (["login", "--org", "beta", "--wait", "inf"], "0 or more"),
    ],
)
def test_login_refuses_a_wait_it_cannot_use(capsys, argv: list[str], said: str) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(argv)
    assert refused.value.code == 2 and said in capsys.readouterr().err


def test_login_with_an_empty_org_refuses(server) -> None:
    with pytest.raises(credentials.LoomError, match="names no org"):
        credentials.login(_SERVER, org="", open_browser=False, echo=lambda _: None)
    assert _signins(server) == []


@pytest.mark.parametrize("wait_s", [float("nan"), float("inf"), -1.0])
def test_login_with_an_org_refuses_a_wait_it_cannot_honour(server, wait_s: float) -> None:
    with pytest.raises(credentials.LoomError, match="wait_s"):
        credentials.login(
            _SERVER, org="beta", wait_s=wait_s, open_browser=False, echo=lambda _: None
        )
    assert _signins(server) == [] and "/cells/lookup" not in [p for p, _ in server.sent]


@pytest.mark.parametrize(
    "header,wait",
    [("7", 7.0), ("inf", 1.0), ("nan", 1.0), ("soon", 1.0), ("600", 60.0), ("0", 1.0)],
)
def test_the_routers_retry_after_is_read_as_the_transport_reads_it(
    server, waits, header: str, wait: float
) -> None:
    _lookup(
        server,
        httpx.Response(429, headers={"Retry-After": header}),
        httpx.Response(200, json={"org": "beta", "cell": "eu-2", "host": "eu-2.example"}),
    )
    assert credentials.org_server(_SERVER, "beta") == "https://eu-2.example"
    assert waits == [wait]


def test_login_refuses_a_wait_it_cannot_honour_with_no_org_too(server) -> None:
    with pytest.raises(credentials.LoomError, match="wait_s"):
        credentials.login(_SERVER, wait_s=float("nan"), open_browser=False, echo=lambda _: None)
    assert _signins(server) == []
