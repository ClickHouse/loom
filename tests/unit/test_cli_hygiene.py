"""The CLI accepts the flags its help advertises, and warns on
the dev secret.

- `loom migrate --status` (and --dry-run/--strict/--target/--migrations-dir)
  reaches `server.core.schema.migrate.main` with exactly those flags; an
  argparse.REMAINDER positional would reject any leading `--flag`.
- `loom mint-token` prints a stderr warning when the signing secret is the
  checked-in dev default and auth is on; stdout stays the bare token.
- `loom doctor` grades that state WARN (exit 0), not PASS; an empty secret
  is still FAIL.
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import datetime
from typing import Any

import jwt as pyjwt
import pytest

import server.cli as cli
from server.auth import keyring
from server.cli import access, doctor, serve
from server.core.secrets_at_rest import seal
from server.settings import DEV_JWT_SECRET, settings
from tests.unit.test_cli_doctor import _passing_httpx_client


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_main(argv: list[str] | None = None) -> int:
        calls.append(list(argv or []))
        return 0

    monkeypatch.setattr("server.core.schema.migrate.main", fake_main)
    return calls


@pytest.mark.parametrize(
    ("cli_argv", "expected"),
    [
        (["migrate", "--status"], ["--status"]),
        (["migrate", "--dry-run", "--strict"], ["--dry-run", "--strict"]),
        (["migrate", "--target", "14"], ["--target", "14"]),
        (
            ["migrate", "--status", "--migrations-dir", "x/y"],
            ["--status", "--migrations-dir", "x/y"],
        ),
        (["migrate"], []),
    ],
)
def test_migrate_forwards_the_flags_its_help_advertises(
    forwarded: list[list[str]], cli_argv: list[str], expected: list[str]
) -> None:
    assert cli.main(cli_argv) == 0
    assert forwarded == [expected]


def test_migrate_rejects_a_flag_the_runner_does_not_have(forwarded: list[list[str]]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["migrate", "--bogus"])
    assert exc.value.code == 2
    assert forwarded == []


def test_migrate_forwarded_argv_parses_in_the_runner(tmp_path) -> None:
    """Drift guard: every flag `loom migrate` mirrors is one `server.core.schema.migrate`
    accepts — its parser must get past argparse (a missing dir is the first
    check after parsing, exit 2 with the ✗ line, not an argparse error)."""
    from server.core.schema.migrate import main as migrate_main

    ns = argparse.Namespace(
        status=True,
        dry_run=True,
        strict=True,
        target=14,
        migrations_dir=str(tmp_path / "missing"),
    )
    argv = serve._migrate_argv(ns)
    assert argv == [
        "--status",
        "--dry-run",
        "--strict",
        "--target",
        "14",
        "--migrations-dir",
        str(tmp_path / "missing"),
    ]
    assert migrate_main(argv) == 2


# ---- mint-token / doctor: the dev secret -----------------------------------

_MINT = ["mint-token", "--subject", "a@x", "--org", "o", "--namespaces", "n"]


class _Keyring:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        assert "loom.signing_keys" in sql
        return self.rows


def _keyring_rows(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> None:
    """`loom mint-token` reads this keyring instead of ClickHouse."""
    monkeypatch.setattr(access, "run_connected", lambda run: asyncio.run(run(_Keyring(rows))))
    monkeypatch.setattr(keyring, "_view", keyring._View())


def _auth_on_with(monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    monkeypatch.setattr(settings, "jwt_secret", secret)
    _keyring_rows(monkeypatch, [])
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")


def test_mint_token_warns_on_stderr_when_dev_secret_and_auth_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _auth_on_with(monkeypatch, DEV_JWT_SECRET)
    assert cli.main(_MINT) == 0
    out, err = capsys.readouterr()
    assert "WARNING" in err and "LOOM_JWT_SECRET is the dev default" in err
    assert out.count("\n") == 1 and "WARNING" not in out  # stdout is still the bare token


def test_mint_token_silent_with_a_real_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _auth_on_with(monkeypatch, "a-real-secret-that-is-not-the-default")
    assert cli.main(_MINT) == 0
    assert capsys.readouterr().err == ""


def test_mint_token_silent_in_single_user_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No bearer is checked in single-user mode, so the secret's strength is
    moot — the warning would be noise on every dev box."""
    _auth_on_with(monkeypatch, DEV_JWT_SECRET)
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    assert cli.main(_MINT) == 0
    assert capsys.readouterr().err == ""


def test_mint_token_signs_with_the_keyring_it_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _auth_on_with(monkeypatch, "a-real-secret-that-is-not-the-default")
    row = {
        "kid": "k1",
        "algorithm": "HS256",
        "sealed_key": seal("signing_key", "k" * 40),
        "public_pem": "",
        "activate_at": datetime(2026, 1, 1),
        "retired": False,
    }
    _keyring_rows(monkeypatch, [row])
    assert cli.main(_MINT) == 0
    token = capsys.readouterr().out.strip()
    assert pyjwt.get_unverified_header(token)["kid"] == "k1"
    pyjwt.decode(token, "k" * 40, algorithms=["HS256"], audience=settings.jwt_audience)


def test_an_offline_mint_signs_with_the_root_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _auth_on_with(monkeypatch, "a-real-secret-that-is-not-the-default")

    def _unreachable(run: Any) -> None:
        raise ConnectionError("no clickhouse")

    monkeypatch.setattr(access, "run_connected", _unreachable)
    assert cli.main(_MINT) == 0
    out, err = capsys.readouterr()
    assert "signing keyring not read; signing as kid" in err
    assert pyjwt.get_unverified_header(out.strip())["kid"] == "default"


def _doctor(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    monkeypatch.setattr(settings, "ch_host", "ch.example.com")
    monkeypatch.setattr(settings, "ch_port", 8443)
    monkeypatch.setattr("httpx.Client", _passing_httpx_client)
    rc = doctor._cmd_doctor(argparse.Namespace())
    return rc, capsys.readouterr().out


def test_doctor_grades_the_dev_secret_warn_not_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _auth_on_with(monkeypatch, DEV_JWT_SECRET)
    rc, out = _doctor(monkeypatch, capsys)
    assert rc == 0  # a warning does not fail the boot preflight
    assert "[WARN]  jwt_secret" in out and "[PASS]  jwt_secret" not in out
    assert "dev default" in out and "LOOM_JWT_SECRET" in out
    # The count and the other warnings vary with the tree (the live provider
    # check, the problem list); the dev secret must be among the warnings.
    assert re.search(r"All \d+ checks passed\. \(\d+ warning\(s\): [^)]*jwt_secret", out), out


def test_doctor_passes_a_real_secret_and_fails_an_empty_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _auth_on_with(monkeypatch, "a-real-secret")
    rc, out = _doctor(monkeypatch, capsys)
    # Other warnings may exist (the problem list on a fake stack);
    # the secret must not be among them.
    assert rc == 0 and "[PASS]  jwt_secret" in out
    assert not re.search(r"warning\(s\): [^)]*jwt_secret", out), out
    _auth_on_with(monkeypatch, "")
    rc, out = _doctor(monkeypatch, capsys)
    assert rc == 1 and "[FAIL]  jwt_secret" in out


def test_report_body_carries_the_warn_grade(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli.doctor import _build_report_body

    _auth_on_with(monkeypatch, DEV_JWT_SECRET)
    monkeypatch.setattr(settings, "ch_host", "ch.example.com")
    monkeypatch.setattr("httpx.Client", _passing_httpx_client)
    body = _build_report_body()
    assert "[WARN]  jwt_secret" in body
    assert DEV_JWT_SECRET not in body.replace(f"({DEV_JWT_SECRET!r})", "")  # only in the hint


def test_no_pid_files_are_tracked() -> None:
    import subprocess
    from pathlib import Path

    root = Path(cli.__file__).resolve().parents[2]
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "*.pid"], capture_output=True, text=True
    )
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")
    assert tracked.stdout.strip() == ""
    assert "*.pid" in (root / ".gitignore").read_text().splitlines()


@pytest.mark.parametrize(
    ("ns", "tag"),
    [
        (argparse.Namespace(cmd="rechunk"), "cli.rechunk"),
        (argparse.Namespace(cmd="code", code_cmd="stats"), "cli.code.stats"),
        (argparse.Namespace(cmd="config", subcommand="get"), "cli.config.get"),
        (argparse.Namespace(cmd="decisions", subcommand="extract"), "cli.decisions.extract"),
    ],
)
def test_dispatch_tags_every_clickhouse_query_with_the_command(
    ns: argparse.Namespace, tag: str
) -> None:
    import asyncio

    from server.cli.app import _dispatch
    from server.core.ch import current_query_tag

    seen: list[str] = []

    async def _query() -> None:
        seen.append(current_query_tag())

    def _func(_: argparse.Namespace) -> int:
        asyncio.run(_query())
        return 0

    ns.func = _func
    assert _dispatch(ns) == 0
    assert seen == [tag]
    assert current_query_tag() == ""


def test_serve_runs_with_no_command_context_bound() -> None:
    """The server's writes and reads bind their own actor, class and tag; a
    process-wide `process:cli:serve` would outrank every worker's fallback."""
    from server.cli.app import _dispatch
    from server.core.ch import current_query_tag
    from server.memory.app.provenance import writer_actor
    from server.observability.traffic_class import current_traffic_class

    seen: list[tuple[str, str, str]] = []

    def _serve(_: argparse.Namespace) -> int:
        seen.append(
            (writer_actor(fallback="process:worker"), current_traffic_class(), current_query_tag())
        )
        return 0

    assert _dispatch(argparse.Namespace(cmd="serve", func=_serve)) == 0
    assert seen == [("process:worker", "", "")]


def test_a_command_refused_for_another_deployment_prints_one_line_and_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from server.cli.app import _dispatch
    from server.core.pg import AuthorityMismatch

    def _func(_: argparse.Namespace) -> int:
        raise AuthorityMismatch("this deployment's memory authority is postgres")

    assert _dispatch(argparse.Namespace(cmd="decisions", func=_func)) == 2
    assert capsys.readouterr().err.strip().splitlines() == [
        "✗ this deployment's memory authority is postgres"
    ]


def test_a_platform_admin_mint_for_another_deployment_is_refused_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not as a fleet state that could not be read: the refusal names the configuration."""
    from server.cli import access
    from server.core.pg import AuthorityMismatch

    def _refuse(_run: object) -> bool:
        raise AuthorityMismatch("this deployment's memory authority is postgres")

    monkeypatch.setattr(access, "run_connected", _refuse)
    with pytest.raises(AuthorityMismatch):
        access._platform_admin_rollout_ready()


def test_a_revocation_for_another_deployment_is_refused_as_such(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one-line refusal and exit 2, not "revocation not recorded"."""
    from server.cli import access, main
    from server.core.pg import AuthorityMismatch

    def _refuse(_run: object) -> int:
        raise AuthorityMismatch("this deployment's memory authority is postgres")

    monkeypatch.setattr(access, "run_connected", _refuse)
    assert main(["revoke-token", "--jti", "j", "--reason", "leaked"]) == 2
    assert "memory authority is postgres" in capsys.readouterr().err
