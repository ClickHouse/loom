"""A token is saved where the help says it lives, can be
revoked by name, and an unreachable server is named in the error.

- `loom mint-token --save [PATH]` writes `~/.loom/token` (or PATH) 0600 with
  a 0700 `~/.loom`, prints the path and never the token; without `--save`
  stdout is still the bare token.
- `loom revoke-token --jti|--subject` goes through `server.auth.revocation`
  (one insert and a local snapshot update) so `is_revoked` flips at once;
  `--list` prints the unexpired rows.
- The Python SDK raises `BackendUnavailable("cannot reach <base_url>: …",
  code="NETWORK")` for a connection or timeout failure instead of leaking the
  httpx exception, and reads `~/.loom/token` after `LOOM_API_KEY`.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from loom import Agent, BackendUnavailable
from loom.transport import Transport, token_from_file

import server.cli as cli
from server.auth import revocation
from server.cli import access
from server.settings import settings
from tests.unit.revocation_fakes import revocation_read

pytestmark = pytest.mark.usefixtures("stub_model_command")

_MINT = ["mint-token", "--subject", "a@x", "--org", "o", "--namespaces", "n"]


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A private HOME so `~/.loom/token` is this test's file."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("LOOM_API_KEY", "LOOM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


class _NoKeys:
    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []


@pytest.fixture
def quiet_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real secret and an empty keyring, so stderr carries neither the
    dev-secret warning nor a keyring read from a live ClickHouse."""
    monkeypatch.setattr(settings, "jwt_secret", "a-real-secret-that-is-not-the-default")
    monkeypatch.setattr(access, "run_connected", lambda run: asyncio.run(run(_NoKeys())))


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# ---- mint-token --save -------------------------------------------------------


def test_mint_save_writes_the_help_path_0600_and_prints_the_path_not_the_token(
    home: Path, quiet_secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([*_MINT, "--save"]) == 0
    out, err = capsys.readouterr()
    token_path = home / ".loom" / "token"
    assert out.strip() == str(token_path)
    token = token_path.read_text().strip()
    assert token.count(".") == 2 and len(token) > 40  # a JWT landed in the file
    assert token not in out and token not in err
    assert _mode(token_path) == 0o600
    assert _mode(token_path.parent) == 0o700
    # No exp: the note on how to retire it goes to stderr, not stdout.
    assert "never expires" in err and "loom revoke-token --subject a@x" in err


def test_mint_save_takes_a_custom_path_and_json_reports_the_path_only(
    home: Path, quiet_secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    target = home / "creds" / "bot.jwt"
    assert cli.main([*_MINT, "--ttl-days", "30", "--save", str(target), "--json"]) == 0
    out, err = capsys.readouterr()
    summary = json.loads(out)
    assert summary["token_path"] == str(target)
    assert "token" not in summary
    assert target.read_text().strip() not in out
    assert _mode(target) == 0o600
    assert err == ""  # a TTL was given: nothing to warn about


def test_mint_save_retightens_an_existing_loose_file(
    home: Path, quiet_secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`loom mint-token > ~/.loom/token` leaves the file at the umask; --save
    over it must end 0600 and hold only the new token."""
    loom_dir = home / ".loom"
    loom_dir.mkdir(mode=0o755)
    old = loom_dir / "token"
    old.write_text("stale\n")
    os.chmod(old, 0o644)
    assert cli.main([*_MINT, "--save"]) == 0
    assert _mode(old) == 0o600
    assert old.read_text().strip() != "stale"
    assert _mode(loom_dir) == 0o755  # an existing directory keeps its mode


def test_mint_without_save_still_prints_the_bare_token(
    home: Path, quiet_secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(_MINT) == 0
    out, err = capsys.readouterr()
    assert out.count("\n") == 1 and out.strip().count(".") == 2
    assert err == ""
    assert not (home / ".loom").exists()


def test_mint_save_unwritable_path_fails_without_leaking_the_token(
    home: Path, quiet_secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = home / "file"
    blocker.write_text("x")
    assert cli.main([*_MINT, "--save", str(blocker / "token")]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "could not write" in err and "eyJ" not in err


def test_ttl_help_recommends_an_expiry_without_changing_the_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")  # argparse wraps at hyphens on a narrow terminal
    with pytest.raises(SystemExit) as exc:
        cli.main(["mint-token", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "Omit for no expiry (the default" in help_text
    assert "--ttl-days 30" in help_text and "loom revoke-token" in help_text
    assert "--save [PATH]" in help_text and "~/.loom/token" in help_text


# ---- revoke-token via the revocation module ---------------------------------


class _FakeCH:
    """Capture revocation and audit writes, and answer revocation list reads."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.connected = 0
        self.disconnected = 0
        self.queries: list[str] = []

    async def connect(self) -> None:
        self.connected += 1

    async def disconnect(self) -> None:
        self.disconnected += 1

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        return revocation_read(self.rows, sql, params)

    async def insert(self, table: str, rows: list[dict[str, Any]], *a: Any, **kw: Any) -> None:
        self.inserts.append((table, rows))
        self.rows.extend(rows)

    async def query_all(self, sql: str, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        self.queries.append(" ".join(sql.split()))
        now = datetime.now(UTC).replace(tzinfo=None)
        return [r for r in self.rows if r["expires_at"] > now]


@pytest.fixture
def fake_ch(monkeypatch: pytest.MonkeyPatch) -> _FakeCH:
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)
    ch = _FakeCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    # audit binds get_client at import, so the module attribute is patched too.
    monkeypatch.setattr("server.observability.audit.get_client", lambda: ch)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    return ch


def test_revoke_by_jti_goes_through_the_module_and_flips_is_revoked(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    assert not revocation.is_revoked("abc-123")
    rc = cli.main(["revoke-token", "--jti", "abc-123", "--reason", "laptop lost"])
    assert rc == 0
    assert [t for t, _ in fake_ch.inserts] == ["loom.revoked_tokens", "loom.audit_log"]
    (row,) = fake_ch.inserts[0][1]
    assert row["jti"] == "abc-123" and row["reason"] == "laptop lost"
    # The CLI leaves a durable record of what it revoked: identifiers only.
    (audit_row,) = fake_ch.inserts[1][1]
    assert audit_row["event_type"] == "token.revoked"
    assert audit_row["actor"].startswith("cli:")
    recorded = json.loads(audit_row["subject"])
    assert recorded["token_id"] == "abc-123" and recorded["matched_claim"] == "jti"
    assert row["revoked_by"].startswith("cli:")
    # No exp on the token → the row outlives it by years, inside the TTL's range.
    assert timedelta(days=3000) < row["expires_at"] - row["revoked_at"]
    assert row["expires_at"] < datetime(2106, 1, 28)  # inside the TTL's DateTime range
    assert revocation.is_revoked("abc-123")
    assert fake_ch.connected == 1 and fake_ch.disconnected == 1
    out, err = capsys.readouterr()
    assert "revoked jti 'abc-123'" in out and "replicas pick it up within" in out
    assert err == ""


def test_revoke_by_subject_records_the_exp_and_says_it_is_org_wide(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        ["revoke-token", "--subject", "bot@x", "--expires-at", "2027-01-31T00:00:00Z", "--json"]
    )
    assert rc == 0
    (row,) = fake_ch.inserts[0][1]
    assert row["jti"] == "bot@x"
    assert row["expires_at"] == datetime(2027, 1, 31)
    assert revocation.is_revoked("bot@x")
    summary = json.loads(capsys.readouterr().out)
    assert summary["matched_claim"] == "subject" and summary["jti"] == "bot@x"


def test_revoke_list_prints_active_rows_from_the_cheap_read(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(revocation.revoke("gone", reason="r1", revoked_by="t"))
    asyncio.run(revocation.revoke("lapsed", expires_at=_utcnow() - timedelta(days=1), reason="r2"))
    capsys.readouterr()
    assert cli.main(["revoke-token", "--list"]) == 0
    out = capsys.readouterr().out
    assert "gone" in out and "r1" in out
    assert "lapsed" not in out  # expired revocations are not active
    assert fake_ch.inserts and all(t == "loom.revoked_tokens" for t, _ in fake_ch.inserts)


def test_revoke_requires_exactly_one_target(fake_ch: _FakeCH) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["revoke-token"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        cli.main(["revoke-token", "--jti", "a", "--subject", "b"])
    assert exc.value.code == 2
    assert fake_ch.inserts == []


def test_revoke_reports_a_failed_insert_as_not_recorded(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    async def boom(*a: Any, **kw: Any) -> None:
        raise ConnectionError("ch is down")

    fake_ch.insert = boom  # type: ignore[method-assign]
    assert cli.main(["revoke-token", "--jti", "x"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and "revocation not recorded" in err and "ch is down" in err
    assert not revocation.is_revoked("x")
    assert fake_ch.disconnected == 1  # the client is released on the failure path too


def test_revoke_rejects_a_bad_expires_at_before_touching_ch(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["revoke-token", "--jti", "x", "--expires-at", "next tuesday"]) == 2
    assert "ISO-8601" in capsys.readouterr().err
    assert fake_ch.connected == 0


def test_module_revoke_requires_a_token_id() -> None:
    with pytest.raises(ValueError):
        asyncio.run(revocation.revoke("  "))


# ---- Python SDK: an unreachable server is a typed error naming the URL -------


def _raising_transport(exc: Exception) -> tuple[Transport, list[int]]:
    t = Transport("http://loom.invalid:7777", api_key="k")
    calls: list[int] = []

    async def post(*a: Any, **kw: Any) -> httpx.Response:
        calls.append(1)
        raise exc

    t._client.post = post  # type: ignore[method-assign]
    return t, calls


@pytest.mark.parametrize(
    ("exc", "where"),
    [
        (httpx.ConnectError("[Errno 111] Connection refused"), "cannot reach"),
        (httpx.ConnectTimeout("timed out"), "cannot reach"),
        (httpx.ReadTimeout("slow"), "no response from"),
    ],
    ids=["ConnectError", "ConnectTimeout", "ReadTimeout"],
)
async def test_sdk_wraps_httpx_network_errors_naming_the_base_url(
    exc: Exception, where: str
) -> None:
    t, calls = _raising_transport(exc)
    try:
        with pytest.raises(BackendUnavailable) as info:
            await t.post(
                "/v1/memory.get",
                {},
                retry={"retry_connection_errors": False, "retry_timeouts": "never"},
            )
    finally:
        await t.close()
    err = info.value
    assert str(err).startswith(f"{where} http://loom.invalid:7777: {type(exc).__name__}")
    assert str(exc) in str(err)
    assert err.code == "NETWORK" and err.status == 0
    assert err.__cause__ is exc
    assert err.request_id  # the id this SDK sent, for the server-side ledger
    assert len(calls) == 1
    assert not isinstance(err, httpx.HTTPError)  # nothing httpx leaks past the SDK boundary


async def test_sdk_wraps_after_the_retries_are_spent_too() -> None:
    t, calls = _raising_transport(httpx.ConnectError("refused"))
    try:
        with pytest.raises(BackendUnavailable, match="cannot reach http://loom.invalid:7777"):
            await t.post(
                "/v1/memory.get",
                {},
                retry={"max_attempts": 2, "backoff_s": 0.0, "backoff_max_s": 0.0},
            )
    finally:
        await t.close()
    assert len(calls) == 2


def test_agent_surfaces_the_typed_error_end_to_end() -> None:
    """Through `Agent`, not just `Transport`: a wrong port is one line naming
    the URL, catchable as `LoomError`, with no httpx type in the catch."""
    agent = Agent(
        org="o",
        namespace="n",
        agent="a",
        api_key="k",
        base_url="http://127.0.0.1:9",
        retry={"max_attempts": 1},
    )
    try:
        with pytest.raises(BackendUnavailable, match="cannot reach http://127.0.0.1:9: ") as info:
            asyncio.run(agent.memory.get(memory_key="k"))
    finally:
        asyncio.run(agent.close())
    assert info.value.code == "NETWORK"


# ---- Python SDK: credential precedence with the token file last -------------


def _auth_header(agent: Agent) -> str | None:
    return agent.transport._client.headers.get("Authorization")


def _with_agent(**kw: Any) -> str | None:
    agent = Agent(org="o", namespace="n", agent="a", **kw)
    try:
        return _auth_header(agent)
    finally:
        asyncio.run(agent.close())


def test_token_file_is_the_last_fallback(home: Path) -> None:
    (home / ".loom").mkdir()
    (home / ".loom" / "token").write_text("tok-from-file\n")
    assert _with_agent() == "Bearer tok-from-file"


def test_env_and_kwarg_still_beat_the_file(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (home / ".loom").mkdir()
    (home / ".loom" / "token").write_text("tok-from-file\n")
    assert _with_agent() == "Bearer tok-from-file"
    monkeypatch.setenv("LOOM_API_KEY", "tok-canonical")
    assert _with_agent() == "Bearer tok-canonical"
    assert _with_agent(api_key="tok-kwarg") == "Bearer tok-kwarg"


def test_missing_or_blank_file_means_no_credential(home: Path) -> None:
    assert _with_agent() is None
    (home / ".loom").mkdir()
    (home / ".loom" / "token").write_text("\n\n")
    assert _with_agent() is None
    assert token_from_file(home / "nope") is None


def test_mint_save_then_agent_round_trip(
    home: Path, quiet_secret: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The writer and the reader agree on the path: what `--save` writes is
    what an `Agent()` with no credential sends."""
    assert cli.main([*_MINT, "--save"]) == 0
    capsys.readouterr()
    token = (home / ".loom" / "token").read_text().strip()
    assert _with_agent() == f"Bearer {token}"
