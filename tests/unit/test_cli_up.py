"""`loom up`: one command boots the stack, mints the first
token when auth is on, and prints how to connect. Every subprocess, HTTP
call and sleep is a module-level seam in `server.cli.up`, replaced here."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import server.cli as cli
import server.cli.up as up

COMPOSE_TEXT = """\
services:
  clickhouse:
    image: clickhouse/clickhouse-server:26.5.1.882
    container_name: loom-ch
  loom:
    build:
      context: ..
    container_name: loom-server
volumes:
  loom-ch-data:
"""

OK_BODY: dict[str, Any] = {"status": "ok", "build_commit": "abc1234", "pending_migrations": []}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".env.example").write_text(
        "# stock\nCOMPOSE_PROJECT_NAME=\nLOOM_AUTH_REQUIRED=false\nLOOM_IDENTITY_PROVIDER=\n"
        "LOOM_JWT_SECRET=\nLOOM_DOCUMENT_EXTRACTOR_TOKEN=\nLOOM_LLM_PROVIDER=fake\n"
    )
    cfg = tmp_path / "deploy" / "clickhouse-config"
    cfg.mkdir(parents=True)
    (cfg / "named_collections.xml.template").write_text("<clickhouse/>\n")
    (tmp_path / "compose.yaml").write_text(COMPOSE_TEXT)
    compose = tmp_path / "dev" / "docker-compose.dev.yml"
    compose.parent.mkdir()
    compose.write_text(COMPOSE_TEXT)
    return tmp_path


class FakeStack:
    """Records the docker commands `loom up` runs and answers health from a
    scripted sequence of (status_code, body) — one entry per poll, the last
    one repeating."""

    def __init__(
        self, health: list[tuple[int, dict[str, Any] | None]], *, mint_stdout: str = "TOK.EN"
    ):
        self.health = list(health)
        self.calls: list[list[str]] = []
        self.mint_stdout = mint_stdout
        self.now = 0.0
        self.slept: list[float] = []

    def run(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "exec"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.mint_stdout + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def http(self, url: str, timeout: float) -> tuple[int, dict[str, Any] | None]:
        if len(self.health) > 1:
            return self.health.pop(0)
        return self.health[0]

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.now += s

    def clock(self) -> float:
        return self.now


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch):
    def _install(health: list[tuple[int, dict[str, Any] | None]], **kw: Any) -> FakeStack:
        fake = FakeStack(health, **kw)
        monkeypatch.setattr(up, "_run", fake.run)
        monkeypatch.setattr(up, "_http_get_json", fake.http)
        monkeypatch.setattr(up, "_sleep", fake.sleep)
        monkeypatch.setattr(up, "_clock", fake.clock)
        monkeypatch.setattr(up.shutil, "which", lambda name: "/usr/bin/docker")
        monkeypatch.setattr(up, "_git_short_head", lambda root: "deadbee")
        return fake

    return _install


def _args(root: Path, **over: Any) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    up.register_up_cli(sub)
    argv = ["up", "--root", str(root)]
    for k, v in over.items():
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        else:
            argv += [flag, str(v)]
    return parser.parse_args(argv)


def _down_args(root: Path, **over: Any) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    up.register_up_cli(sub)
    argv = ["down", "--root", str(root)]
    for k, v in over.items():
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        else:
            argv += [flag, str(v)]
    return parser.parse_args(argv)


# ---- bootstrap ----------------------------------------------------------------


def test_bootstrap_creates_env_and_named_collections_once(root: Path) -> None:
    created = up.bootstrap(root, ".env")
    env = up.read_env_file(root / ".env")
    assert len(env["LOOM_JWT_SECRET"]) >= 40
    assert len(env["LOOM_DOCUMENT_EXTRACTOR_TOKEN"]) == 48  # the extractor refuses under 16
    assert env["COMPOSE_PROJECT_NAME"].startswith("loom-local-")
    assert (root / ".env").stat().st_mode & 0o777 == 0o600  # it will hold secrets
    assert (root / up.NAMED_COLLECTIONS).read_text() == "<clickhouse/>\n"
    assert [c.split(" from")[0] for c in created] == [
        "created .env",
        f"created {up.NAMED_COLLECTIONS.as_posix()}",
    ]
    (root / ".env").write_text("LOOM_AUTH_REQUIRED=true\n")
    assert up.bootstrap(root, ".env") == []  # second run: nothing overwritten
    assert (root / ".env").read_text() == "LOOM_AUTH_REQUIRED=true\n"


def test_repo_root_prefers_explicit_then_cwd(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert up.repo_root(str(root)) == root.resolve()
    monkeypatch.chdir(root)
    assert up.repo_root(None) == root.resolve()
    empty = tmp_path / "elsewhere"
    empty.mkdir()
    assert up.repo_root(str(empty)) is None


# ---- mode detection from .env -------------------------------------------------


@pytest.mark.parametrize(
    ("env", "mode"),
    [
        ({}, "single-user"),
        ({"LOOM_AUTH_REQUIRED": "false", "LOOM_IDENTITY_PROVIDER": ""}, "single-user"),
        ({"LOOM_AUTH_REQUIRED": "true"}, "trust-network"),  # compose pins LOOM_SELF_SIGNUP=open
        ({"LOOM_AUTH_REQUIRED": "true", "LOOM_IDENTITY_PROVIDER": "gateway"}, "gateway"),
        ({"LOOM_AUTH_REQUIRED": "true", "LOOM_IDENTITY_PROVIDER": "single-user"}, "single-user"),
    ],
)
def test_stack_mode_mirrors_effective_identity_provider(env: dict[str, str], mode: str) -> None:
    assert up.stack_mode(env) == mode


def test_read_env_file_skips_comments_and_strips_quotes(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        "# c\n\nLOOM_AUTH_REQUIRED=\"true\"\nexport LOOM_DEFAULT_NAMESPACE='team'\nBROKEN LINE\n"
    )
    assert up.read_env_file(p) == {"LOOM_AUTH_REQUIRED": "true", "LOOM_DEFAULT_NAMESPACE": "team"}
    assert up.read_env_file(tmp_path / "missing") == {}


def test_compose_project_is_checkout_unique_and_env_file_is_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    first = tmp_path / "one" / "loom"
    second = tmp_path / "two" / "loom"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    assert up.compose_project_name(first, ".env") != up.compose_project_name(second, ".env")

    (first / ".env").write_text("COMPOSE_PROJECT_NAME=my-local-loom\n")
    assert up.compose_project_name(first, ".env") == "my-local-loom"
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "shell-wins")
    assert up.compose_project_name(first, ".env") == "my-local-loom"


def test_compose_names_reads_the_loom_service_container() -> None:
    assert up.compose_names(COMPOSE_TEXT) == ("loom", "loom-server")
    renamed = COMPOSE_TEXT.replace("  loom:\n", "  api:\n").replace("loom-server", "acme-loom")
    assert up.compose_names(renamed) == ("api", "acme-loom")
    assert up.compose_names("services: {}\n") == ("loom", "loom-server")


# ---- health wait loop ---------------------------------------------------------


def test_wait_for_health_reports_progress_with_migration_count(stack, capsys) -> None:
    booting = {"status": "degraded", "pending_migrations": ["1000_a.sql", "1010_b.sql"]}
    fake = stack([(0, None), (503, booting), (503, booting), (503, booting), (200, OK_BODY)])
    ok, code, body = up.wait_for_health(
        "http://localhost:7777/v1/health", timeout_s=300, poll_s=2.0
    )
    assert ok and code == 200 and body == OK_BODY
    out = capsys.readouterr().out
    # polls at 0/2/4/6/8 s; the one progress line (t=6 s) names the booting body
    assert out.count("waiting for") == 1
    assert "not answering yet" not in out
    assert "(6 s): status=degraded, pending migrations=2" in out
    assert fake.slept == [2.0, 2.0, 2.0, 2.0]


def test_wait_for_health_times_out_with_last_body(stack) -> None:
    degraded = {"status": "degraded", "ch_error": "ConnectionError: refused"}
    stack([(503, degraded)])
    ok, code, body = up.wait_for_health("http://localhost:7777/v1/health", timeout_s=10, poll_s=2.0)
    assert ok is False and code == 503 and body == degraded


# ---- the command: single-user stock stack -----------------------------------------


def test_up_single_user_boots_and_prints_connect_snippets(root: Path, stack, capsys) -> None:
    fake = stack([(0, None), (0, None), (200, OK_BODY)])
    assert up._cmd_up(_args(root)) == 0
    out = capsys.readouterr().out
    # bootstrap ran, compose up --build ran with --env-file, no mint (auth off)
    assert (root / ".env").exists()
    compose = [c for c in fake.calls if c[:2] == ["docker", "compose"]]
    assert compose == [
        [
            "docker",
            "compose",
            "--project-name",
            up.compose_project_name(root, ".env"),
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "compose.yaml"),
            "-f",
            str(root / "dev/docker-compose.dev.yml"),
            "--profile",
            "bundled",
            "up",
            "-d",
            "--build",
        ]
    ]
    assert not [c for c in fake.calls if c[:2] == ["docker", "exec"]]
    # the connect block
    assert "http://localhost:7777/" in out
    assert "/mcp?toolset=coding" in out
    assert "single-user mode" in out
    assert '"namespace": "local"' in out and 'namespace="local"' in out
    assert "Authorization" not in out  # tokenless snippets
    assert "build abc1234" in out


def test_up_no_build_and_custom_file(root: Path, stack) -> None:
    (root / "dev" / "other.yml").write_text(COMPOSE_TEXT)
    fake = stack([(0, None), (200, OK_BODY)])
    assert up._cmd_up(_args(root, no_build=True, file="dev/other.yml", port=8888)) == 0
    compose = [c for c in fake.calls if c[:2] == ["docker", "compose"]]
    assert compose == [
        [
            "docker",
            "compose",
            "--project-name",
            up.compose_project_name(root, ".env"),
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "dev/other.yml"),
            "--profile",
            "bundled",
            "up",
            "-d",
        ]
    ]


def test_compose_up_env_file_wins_over_loaded_checkout_env(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / ".env").write_text("COMPOSE_PROJECT_NAME=selected-project\nLOOM_AUTH_REQUIRED=false\n")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "wrong-project")
    monkeypatch.setenv("LOOM_AUTH_REQUIRED", "true")
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["env"] = kw["env"]
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(up, "_run", run)
    monkeypatch.setattr(up, "_git_short_head", lambda root: "deadbee")
    assert up.compose_up(root, up.DEFAULT_COMPOSE_FILES, ".env", build=False) == 0
    assert "selected-project" in seen["argv"]
    assert "wrong-project" not in seen["argv"]
    assert "COMPOSE_PROJECT_NAME" not in seen["env"]
    assert "LOOM_AUTH_REQUIRED" not in seen["env"]
    assert seen["env"]["LOOM_BUILD_COMMIT"] == "deadbee"


def test_up_rerun_on_running_stack_reconciles_without_rebuilding(root: Path, stack, capsys) -> None:
    fake = stack([(200, OK_BODY), (200, OK_BODY), (200, OK_BODY), (200, OK_BODY)])
    assert up._cmd_up(_args(root)) == 0
    assert up._cmd_up(_args(root)) == 0
    compose = [c for c in fake.calls if c[:2] == ["docker", "compose"]]
    assert len(compose) == 2
    assert all(c[-5:] == ["up", "-d", "--force-recreate", "--no-deps", "loom"] for c in compose)
    out = capsys.readouterr().out
    assert out.count("reconciling configuration") == 2
    assert out.count("http://localhost:7777/") >= 2


def test_up_rebuild_runs_compose_even_when_healthy(root: Path, stack) -> None:
    fake = stack([(200, OK_BODY)])
    assert up._cmd_up(_args(root, rebuild=True)) == 0
    assert [c for c in fake.calls if c[:2] == ["docker", "compose"]][0][-6:] == [
        "up",
        "-d",
        "--build",
        "--force-recreate",
        "--no-deps",
        "loom",
    ]


def test_up_timeout_prints_last_body_and_tails_logs(root: Path, stack, capsys) -> None:
    degraded = {"status": "degraded", "pending_migrations": ["1000_a.sql"], "ch_error": "refused"}
    fake = stack([(0, None), (503, degraded)])
    assert up._cmd_up(_args(root, timeout=7)) == 1
    err = capsys.readouterr().err
    assert "did not report status=ok within 7 s" in err
    assert '"ch_error": "refused"' in err
    logs = [c for c in fake.calls if "logs" in c]
    assert logs == [
        [
            "docker",
            "compose",
            "--project-name",
            up.compose_project_name(root, ".env"),
            "--env-file",
            str(root / ".env"),
            "-f",
            str(root / "compose.yaml"),
            "-f",
            str(root / "dev/docker-compose.dev.yml"),
            "--profile",
            "bundled",
            "logs",
            "--tail",
            "40",
            "loom",
        ]
    ]


def test_up_compose_failure_returns_its_exit_code(
    root: Path, stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack([(0, None)])

    def failing(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 17, stdout="", stderr="")

    monkeypatch.setattr(up, "_run", failing)
    assert up._cmd_up(_args(root)) == 17


def test_up_without_docker_or_checkout_is_actionable(
    root: Path, tmp_path: Path, stack, capsys, monkeypatch
) -> None:
    stack([(0, None)])
    monkeypatch.setattr(up.shutil, "which", lambda name: None)
    assert up._cmd_up(_args(root)) == 2
    assert "docker" in capsys.readouterr().err
    monkeypatch.setattr(up.shutil, "which", lambda name: "/usr/bin/docker")
    empty = tmp_path / "nowhere"
    empty.mkdir()
    assert up._cmd_up(_args(empty)) == 2
    assert "git clone" in capsys.readouterr().err


def test_up_open_launches_the_dashboard(root: Path, stack, monkeypatch: pytest.MonkeyPatch) -> None:
    stack([(200, OK_BODY)])
    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    assert up._cmd_up(_args(root, open=True)) == 0
    assert opened == ["http://localhost:7777/"]


# ---- the command: auth on ---------------------------------------------------------


def test_up_auth_on_mints_through_the_container_and_prints_token(root: Path, stack, capsys) -> None:
    (root / ".env").write_text("LOOM_AUTH_REQUIRED=true\nLOOM_DEFAULT_NAMESPACE=team\n")
    fake = stack([(0, None), (200, OK_BODY)], mint_stdout="  ! WARNING: dev secret\nEYJ.TOKEN")
    assert up._cmd_up(_args(root, subject="ana@example.com", org="acme")) == 0
    out = capsys.readouterr().out
    mint = [c for c in fake.calls if c[:2] == ["docker", "exec"]]
    assert mint == [
        [
            "docker",
            "exec",
            "loom-server",
            "loom",
            "mint-token",
            "--subject",
            "ana@example.com",
            "--org",
            "acme",
            "--namespaces",
            "team",
            "--roles",
            "writer",
        ]
    ]
    assert "trust-network mode" in out
    assert "EYJ.TOKEN" in out and up.TOKEN_FILE in out
    # `_shell_words` double-quotes the URL (it carries `?`) and the header
    assert (
        'claude mcp add --transport http loom "http://localhost:7777/mcp?toolset=coding" '
        '-H "Authorization: Bearer EYJ.TOKEN" -s user'
    ) in out
    assert 'api_key=os.environ["LOOM_API_KEY"]' in out
    assert 'namespace="team"' in out and '"org": "acme"' in out


def test_up_auth_on_mint_failure_prints_the_manual_command(
    root: Path, stack, capsys, monkeypatch
) -> None:
    (root / ".env").write_text("LOOM_AUTH_REQUIRED=true\n")
    fake = stack([(200, OK_BODY)])

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        fake.calls.append(list(argv))
        if argv[:2] == ["docker", "compose"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such container")

    monkeypatch.setattr(up, "_run", run)
    assert up._cmd_up(_args(root, subject="me")) == 1
    err = capsys.readouterr().err
    assert "no such container" in err
    assert (
        "docker exec loom-server loom mint-token --subject me --org dev --namespaces local --roles writer"
        in err
    )


# ---- wiring -----------------------------------------------------------------------


def test_loom_up_is_registered_on_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(up, "_cmd_up", lambda args: seen.append(args) or 0)
    assert cli.main(["up", "--no-build", "--timeout", "12", "--port", "7778"]) == 0
    assert seen[0].no_build is True and seen[0].timeout == 12 and seen[0].port == 7778
    assert seen[0].file is None and seen[0].env_file == ".env"


def test_down_preserves_data_by_default(root: Path, stack, capsys) -> None:
    (root / ".env").write_text("COMPOSE_PROJECT_NAME=loom-safe-stop\n")
    fake = stack([(200, OK_BODY)])
    assert up._cmd_down(_down_args(root)) == 0
    assert fake.calls == [up.compose_argv(root, up.DEFAULT_COMPOSE_FILES, ".env", "down")]
    assert "data volumes were preserved" in capsys.readouterr().out


def test_down_delete_data_requires_exact_project_confirmation(
    root: Path, stack, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    (root / ".env").write_text("COMPOSE_PROJECT_NAME=loom-confirm-me\n")
    fake = stack([(200, OK_BODY)])
    monkeypatch.setattr("builtins.input", lambda prompt: "wrong-project")
    assert up._cmd_down(_down_args(root, delete_data=True)) == 2
    assert fake.calls == []
    assert "not changed" in capsys.readouterr().err

    monkeypatch.setattr("builtins.input", lambda prompt: "loom-confirm-me")
    assert up._cmd_down(_down_args(root, delete_data=True)) == 0
    assert fake.calls == [
        up.compose_argv(root, up.DEFAULT_COMPOSE_FILES, ".env", "down", "--volumes")
    ]


def test_down_missing_env_refuses_to_guess(root: Path, stack, capsys) -> None:
    stack([(200, OK_BODY)])
    assert up._cmd_down(_down_args(root)) == 2
    assert "refusing to guess" in capsys.readouterr().err


def test_loom_down_is_registered_on_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []
    monkeypatch.setattr(up, "_cmd_down", lambda args: seen.append(args) or 0)
    assert cli.main(["down", "--delete-data", "--yes"]) == 0
    assert seen[0].delete_data is True and seen[0].yes is True


def test_snippets_carry_the_identity_tuple() -> None:
    curl = up._curl_snippet("http://h:1", "dev", "local", with_token=False)
    body = json.loads(curl.split("-d '", 1)[1].rstrip("'"))
    assert body == {
        "org": "dev",
        "namespace": "local",
        "agent": "quickstart",
        "memory_key": "deploy_window",
        "value": "Tue and Thu only; never Friday.",
    }
    assert "Authorization" in up._curl_snippet("http://h:1", "dev", "local", with_token=True)


def test_a_mint_that_never_answers_is_an_error_not_a_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    """`docker exec` against a wedged container blocks forever without a timeout;
    the mint gives up and `loom up` prints the manual command instead."""
    seen: dict[str, Any] = {}

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kw)
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(up, "_run", run)
    token, err = up.mint_token_via_container("loom-server", subject="me", org="dev", namespace="n")
    assert token == ""
    assert "gave no answer" in err
    assert seen["timeout"] == up._MINT_TIMEOUT_S
