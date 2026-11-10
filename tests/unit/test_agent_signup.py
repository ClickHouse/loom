"""An agent signs itself up.

The route provisions `agent-<slug>` with the human self-signup's account
shape (owner = the namespace name, a claim code no sign-in takes), grants
a service-account subject `writer`, mints that subject's token, and is gated
and rate-limited; the CLI saves the token and config where the hooks look
and prints the claim code once. Hermetic: the ClickHouse client is faked.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

import server.auth.self_signup as ss
from server.auth import claim_codes, passwords
from server.auth.jwt import verify_token


class _FakeCH:
    def __init__(self, existing: bool = False) -> None:
        self.existing = existing
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return {"x": 1} if self.existing else None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


class _Req:
    def __init__(self, ip: str = "10.0.0.1", headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {"host": "loom.test:7777"}
        self.client = type("C", (), {"host": ip})()
        self.url = type("U", (), {"scheme": "http"})()


@pytest.fixture()
def _open(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any) -> _FakeCH:
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    fake = _FakeCH()
    monkeypatch.setattr(ss, "get_client", lambda: fake)
    return fake


# ---- pure helpers ----


def test_agent_namespace_slugs_and_refuses_the_unusable() -> None:
    assert ss.agent_namespace("Nightly Triage") == "agent-nightly-triage"
    assert ss.agent_namespace("  ci/bot_2  ") == "agent-ci-bot-2"
    assert ss.agent_namespace("x" * 100) == "agent-" + "x" * 56
    assert ss.agent_namespace("---") is None
    assert ss.agent_namespace("") is None


@pytest.mark.asyncio
async def test_rate_limit_is_five_per_address_per_hour(auth_attempts: Any) -> None:
    for _ in range(5):
        assert await ss._agent_signups.allow("1.2.3.4")
        auth_attempts.now += 1
    assert not await ss._agent_signups.allow("1.2.3.4")
    assert await ss._agent_signups.allow("5.6.7.8")  # another address
    auth_attempts.now += 3600 - 4
    assert await ss._agent_signups.allow("1.2.3.4")  # the window slid


def _address_seen(peer: str, xff: str, trusted: str) -> str:
    """client_address behind uvicorn's ProxyHeadersMiddleware, as `loom serve`
    runs it with LOOM_TRUSTED_PROXIES=`trusted`."""
    from starlette.requests import Request
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(ss.client_address(Request(scope)))

    scope = {
        "type": "http",
        "client": (peer, 5000),
        "headers": [(b"x-forwarded-for", xff.encode())],
    }
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))  # type: ignore[arg-type]
    return seen[0]


def test_a_forwarded_for_from_an_untrusted_peer_is_ignored() -> None:
    assert _address_seen("203.0.113.7", "1.1.1.1", trusted="127.0.0.1") == "203.0.113.7"


def test_a_trusted_proxy_names_the_client() -> None:
    # The client wrote 1.1.1.1; the proxy appended the address it saw.
    assert _address_seen("127.0.0.1", "1.1.1.1, 2.2.2.2", trusted="127.0.0.1") == "2.2.2.2"


def test_without_a_proxy_the_peer_is_the_address() -> None:
    assert (
        ss.client_address(_Req(ip="9.9.9.9", headers={"x-forwarded-for": "1.1.1.1"})) == "9.9.9.9"
    )


@pytest.mark.asyncio
async def test_every_process_counts_the_same_attempts(auth_attempts: Any) -> None:
    """The limit is rows of loom.auth_attempts, not a process's memory: a
    limiter built elsewhere (another process) sees the attempts this one
    recorded, and another kind's limit does not."""
    from server.auth.attempts import Attempts

    for _ in range(ss.AGENT_SIGNUPS_PER_HOUR):
        assert await ss._agent_signups.allow("1.2.3.4")
    elsewhere = Attempts("signup.agent", ss.AGENT_SIGNUPS_PER_HOUR, 3600)
    assert not await elsewhere.allow("1.2.3.4")
    assert await ss._human_signups.allow("1.2.3.4")


# ---- the route ----


async def _signup(fake: _FakeCH, **kw: Any) -> ss.AgentSignupOutput:
    body = {"name": "nightly triage", **kw}
    return await ss.agent_signup(ss.AgentSignupInput(**body), _Req())


@pytest.mark.asyncio
async def test_signup_provisions_the_account_shape_and_returns_a_writer_sa_token(
    _open: _FakeCH,
) -> None:
    out = await _signup(_open, purpose="Files the nightly failures.")
    assert out.namespace == "agent-nightly-triage" and out.org == "dev"
    assert re.fullmatch(r"sa:agent-nightly-triage-[0-9a-f]{6}", out.subject)
    assert len(out.claim_code) >= 20
    assert out.claim_url == "http://loom.test:7777/oauth/agent-claim?namespace=agent-nightly-triage"

    claims = verify_token(out.token)
    assert claims.subject == out.subject and claims.org == "dev"
    assert list(claims.namespaces) == ["agent-nightly-triage"]
    assert [r.name.lower() for r in claims.roles] == ["writer"]
    assert claims.is_service_account is True

    tables = [t for t, _ in _open.inserts]
    assert tables == [
        "loom.namespaces",
        "loom.namespace_grants",
        "loom.namespace_grants",
        "loom.service_accounts",
    ]
    ns_row = _open.inserts[0][1][0]
    # the human self-signup shape: the namespace is its own owner identity; the
    # claim code is stored so that only the claim page takes it, never a sign-in
    assert ns_row["owner_email"] == "agent-nightly-triage"
    assert ns_row["contact_email"] == ""
    assert not passwords.verify_password(ns_row["password_hash"], out.claim_code)
    assert await claim_codes.verify_claim_code_off_loop(ns_row["password_hash"], out.claim_code)
    assert not await claim_codes.verify_claim_code_off_loop(ns_row["password_hash"], "not-the-code")
    assert ns_row["tags"] == ["agent"] and ns_row["description"] == "Files the nightly failures."
    owner_grant, sa_grant = _open.inserts[1][1][0], _open.inserts[2][1][0]
    assert (owner_grant["subject"], owner_grant["role"]) == ("agent-nightly-triage", "admin")
    assert (sa_grant["subject"], sa_grant["role"]) == (out.subject, "writer")
    sa_row = _open.inserts[3][1][0]
    assert sa_row["kind"] == "agent" and sa_row["client"] == "agent-signup"
    assert sa_row["inherit_creator_grants"] is False
    assert json.loads(sa_row["ns_roles"]) == {"agent-nightly-triage": "writer"}
    assert sa_row["ttl_days"] == 365 and sa_row["expires_at"] is not None


@pytest.mark.asyncio
async def test_signup_without_ttl_mints_a_token_that_does_not_expire(_open: _FakeCH) -> None:
    out = await _signup(_open, ttl_days=None)
    assert out.expires_at is None
    assert _open.inserts[3][1][0]["ttl_days"] == 0


@pytest.mark.asyncio
async def test_signup_refuses_a_taken_name_and_an_unusable_one(_open: _FakeCH) -> None:
    _open.existing = True
    with pytest.raises(HTTPException) as e:
        await _signup(_open)
    assert e.value.status_code == 409 and e.value.detail["code"] == "NAMESPACE_CLAIMED"
    _open.existing = False
    with pytest.raises(HTTPException) as e:
        await _signup(_open, name="///")
    assert e.value.status_code == 422
    assert _open.inserts == []


@pytest.mark.asyncio
async def test_signup_is_gated_like_the_human_route(
    monkeypatch: pytest.MonkeyPatch, _open: _FakeCH
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "off")
    with pytest.raises(HTTPException) as e:
        await _signup(_open)
    assert e.value.status_code == 404 and e.value.detail["code"] == "SELF_SIGNUP_DISABLED"
    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "oidc")
    with pytest.raises(HTTPException) as e:
        await _signup(_open)
    assert e.value.detail["code"] == "SELF_SIGNUP_SSO"


@pytest.mark.asyncio
async def test_sixth_signup_from_one_address_is_429(_open: _FakeCH) -> None:
    for i in range(5):
        await _signup(_open, name=f"bot-{i}")
    with pytest.raises(HTTPException) as e:
        await _signup(_open, name="bot-6")
    assert e.value.status_code == 429 and e.value.detail["code"] == "RATE_LIMITED"


# ---- the CLI ----


def test_cli_saves_token_and_config_and_prints_the_claim_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import server.cli.init as ci

    seen: dict[str, Any] = {}

    def _fake_post(
        url: str, body: dict[str, Any], timeout: float = 15.0
    ) -> tuple[int, dict[str, Any]]:
        seen["url"], seen["body"] = url, body
        return 200, {
            "token": "T.T.T",
            "namespace": "agent-nightly",
            "org": "dev",
            "subject": "sa:agent-nightly-abc123",
            "claim_code": "CLAIM-CODE-1",
            "claim_url": "https://h/",
            "expires_at": None,
        }

    monkeypatch.setattr(ci, "_post_json", _fake_post)
    tok, cfg = tmp_path / "t" / "token", tmp_path / "t" / "config.json"
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({"agent": "keep"}))
    import argparse

    args = argparse.Namespace(
        agent="nightly",
        server_url="https://h/",
        org=None,
        purpose="p",
        ttl_days=30,
        token_path=str(tok),
        config_path=str(cfg),
        no_files=False,
        json=False,
    )
    assert ci._cmd_init(args) == 0
    assert seen["url"] == "https://h/v1/auth/agent-signup"
    assert seen["body"] == {"name": "nightly", "purpose": "p", "ttl_days": 30}
    assert tok.read_text() == "T.T.T\n" and oct(tok.stat().st_mode & 0o777) == "0o600"
    assert json.loads(cfg.read_text()) == {
        "agent": "keep",
        "url": "https://h",
        "namespace": "agent-nightly",
        "org": "dev",
    }
    assert oct(cfg.stat().st_mode & 0o777) == "0o600"
    out = capsys.readouterr().out
    assert "CLAIM-CODE-1" in out and "agent-nightly" in out
    assert "T.T.T" not in out, "the token is saved, never printed"
    assert "codex mcp add" in out and "claude mcp add" in out
    assert "pip install clickhouse-loom" in out


def test_cli_no_files_json_is_one_parseable_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`init --no-files --json`: the token rides inside the JSON object, not
    as a bare line after it, so stdout parses as one document."""
    import argparse

    import server.cli.init as ci

    reply = {"token": "T.T.T", "namespace": "agent-x", "org": "dev", "claim_code": "C"}
    monkeypatch.setattr(ci, "_post_json", lambda u, b, timeout=15.0: (200, dict(reply)))
    args = argparse.Namespace(
        agent="x",
        server_url="https://h",
        org=None,
        purpose="",
        ttl_days=None,
        token_path="/nonexistent/t",
        config_path="/nonexistent/c",
        no_files=True,
        json=True,
    )
    assert ci._cmd_init(args) == 0
    assert json.loads(capsys.readouterr().out) == reply


def test_cli_explains_each_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import server.cli.init as ci

    cases = [
        (0, {"detail": {"code": "UNREACHABLE", "message": "refused"}}, "could not reach"),
        (
            404,
            {"detail": {"code": "SELF_SIGNUP_DISABLED", "message": "off"}},
            "does not accept agent sign-ups",
        ),
        (409, {"detail": {"code": "NAMESPACE_CLAIMED", "message": "exists"}}, "name taken"),
        (429, {"detail": {"code": "RATE_LIMITED", "message": "5/h"}}, "slow down"),
    ]
    for status, body, needle in cases:
        monkeypatch.setattr(ci, "_post_json", lambda u, b, timeout=15.0, s=status, bd=body: (s, bd))
        args = argparse.Namespace(
            agent="x",
            server_url="https://h",
            org=None,
            purpose="",
            ttl_days=None,
            token_path="/nonexistent/t",
            config_path="/nonexistent/c",
            no_files=True,
            json=False,
        )
        assert ci._cmd_init(args) == 1
        assert needle in capsys.readouterr().err
    args = argparse.Namespace(
        agent="",
        server_url="https://h",
        org=None,
        purpose="",
        ttl_days=None,
        token_path="x",
        config_path="y",
        no_files=True,
        json=False,
    )
    assert ci._cmd_init(args) == 2


def test_init_is_registered_on_the_cli() -> None:
    import argparse

    import server.cli.init as ci

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    ci.register_init_cli(sub)
    ns = parser.parse_args(["init", "--agent", "bot", "--no-files", "--ttl-days", "7"])
    assert ns.agent == "bot" and ns.no_files is True and ns.ttl_days == 7
    assert ns.func is ci._cmd_init
    # and `loom --help` lists it: main() registers the module
    src = "\n".join(p.read_text() for p in sorted(Path(ci.__file__).resolve().parent.glob("*.py")))
    assert "register_init_cli(sub)" in src


def test_post_json_answers_a_read_timeout_and_a_non_json_body(monkeypatch) -> None:
    """Neither a timeout after connect nor an HTML page from a proxy is a
    traceback: both come back as a status the caller explains."""
    import io

    import server.cli._client as cc
    import server.cli.init as ci

    def _timeout(req, timeout):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(cc.urllib.request, "urlopen", _timeout)
    status, body = ci._post_json("http://x/v1/auth/agent-signup", {})
    assert status == 0 and "TimeoutError" in body["detail"]
    assert "could not reach" in ci.explain_failure(status, body, "http://x")

    class _Resp(io.BytesIO):
        status = 200

    monkeypatch.setattr(
        cc.urllib.request, "urlopen", lambda req, timeout: _Resp(b"<html>sign in</html>")
    )
    status, body = ci._post_json("http://x/v1/auth/agent-signup", {})
    assert status == 0 and "token" not in body and "not a JSON answer" in body["detail"]


@pytest.mark.asyncio
async def test_single_user_signup_returns_the_default_identity_with_nothing_to_claim(
    monkeypatch: pytest.MonkeyPatch, auth_attempts: Any
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    monkeypatch.setattr(settings, "self_signup", "closed")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    monkeypatch.setattr(settings, "default_namespace", "local")
    fake = _FakeCH()
    monkeypatch.setattr(ss, "get_client", lambda: fake)

    for _ in range(ss.AGENT_SIGNUPS_PER_HOUR):
        await _signup(fake)
    out = await _signup(fake, ttl_days=30)
    assert (out.namespace, out.org, out.subject, out.claim_code) == ("local", "dev", "you", "")
    claims = verify_token(out.token)
    assert list(claims.namespaces) == ["local"] and claims.is_service_account is False
    assert [r.name.lower() for r in claims.roles] == ["writer"]
    assert fake.inserts == []


@pytest.fixture(autouse=True)
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployment-wide claims, without a lock table in the fake."""
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)
