"""dev/redeploy_preflight.sh: health-sha compare (exit 3 when
the running build already is the target), the port check, and the migrate
dry-run whose exit code decides go/stop. Driven against a throwaway HTTP
server standing in for /v1/health and a stand-in migrate command."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "dev" / "redeploy_preflight.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


class _Health(BaseHTTPRequestHandler):
    build = "abc1234"

    def do_GET(self):
        body = json.dumps({"status": "ok", "build_commit": self.build}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet
        return


@pytest.fixture
def health_server():
    srv = HTTPServer(("127.0.0.1", 0), _Health)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()


def _run(port: int | None, *args: str, migrate: str = "true", extra_env: dict | None = None):
    env = dict(os.environ)
    if port is not None:
        env["LOOM_PREFLIGHT_HEALTH_URL"] = f"http://127.0.0.1:{port}/v1/health"
        env["LOOM_PREFLIGHT_PORT"] = str(port)
    env["LOOM_PREFLIGHT_MIGRATE"] = migrate
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60
    )


def test_script_is_executable_and_parses() -> None:
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_same_build_exits_3_unless_allow_same(health_server: int) -> None:
    r = _run(health_server, "--target", "abc1234")
    assert r.returncode == 3, r.stdout + r.stderr
    assert "already at target" in r.stdout
    # A long SHA that starts with the short running one is the same build.
    assert _run(health_server, "--target", "abc1234def5678").returncode == 3
    r = _run(health_server, "--target", "abc1234", "--allow-same")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "preflight OK" in r.stdout


def test_new_build_runs_the_dry_run_and_passes_when_it_does(health_server: int) -> None:
    r = _run(health_server, "--target", "fff9999", migrate="echo 'ledger-only: 0'")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "running: abc1234" in r.stdout
    # The listener is judged by health, not by process name (in CI ss shows it as pytest).
    assert "the listener is the build being replaced" in r.stdout
    assert "ledger-only: 0" in r.stdout
    # strict by default: the migrate command receives --strict
    assert "--strict" in r.stdout or r.returncode == 0


def test_strict_is_passed_through_and_no_strict_removes_it(health_server: int) -> None:
    r = _run(health_server, "--target", "fff9999", migrate="echo ARGS")
    assert "ARGS --strict" in r.stdout
    r = _run(health_server, "--target", "fff9999", "--no-strict", migrate="echo ARGS")
    assert "ARGS --strict" not in r.stdout and "ARGS" in r.stdout


def test_failing_dry_run_stops_the_deploy(health_server: int) -> None:
    r = _run(health_server, "--target", "fff9999", migrate="sh -c 'echo drift; exit 1'")
    assert r.returncode == 1
    assert "preflight STOP" in r.stdout and "Migrations pending or checksum drift" in r.stdout


def test_port_held_without_health_is_a_stray_process() -> None:
    # A raw listening socket that never answers HTTP: :port is held, health is unreachable.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        r = _run(port, "--target", "fff9999")
    finally:
        s.close()
    assert r.returncode == 1, r.stdout
    assert "stray process holds the port" in r.stdout


def test_nothing_listening_is_a_first_deploy_not_a_failure() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    r = _run(port, "--target", "fff9999")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nobody — first deploy or the server is down" in r.stdout


def test_repo_redeploy_script_calls_the_preflight_and_honours_exit_3() -> None:
    text = (ROOT / "redeploy.sh").read_text()
    assert "dev/redeploy_preflight.sh" in text
    assert "3)" in text and "--allow-same" in text


def test_target_only_migrations_are_listed(health_server: int, tmp_path: Path) -> None:
    """The dry-run reads the checkout's migrations (the build being
    replaced); a migration only on the target is listed so the operator knows
    the new build's boot applies it."""
    repo = tmp_path / "repo"
    (repo / "schemas" / "migrations").mkdir(parents=True)
    (repo / "dev").mkdir()
    shutil.copy(SCRIPT, repo / "dev" / "redeploy_preflight.sh")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo)]
    subprocess.run([*git, "init", "-q"], check=True)
    (repo / "schemas" / "migrations" / "10_a.sql").write_text("SELECT 1;\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "target"], check=True)
    (repo / "schemas" / "migrations" / "20_b.sql").write_text("SELECT 2;\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "adds 20_b"], check=True)
    subprocess.run([*git, "checkout", "-q", "-"], check=True)  # the checkout lacks 20_b
    env = dict(os.environ)
    env["LOOM_PREFLIGHT_HEALTH_URL"] = f"http://127.0.0.1:{health_server}/v1/health"
    env["LOOM_PREFLIGHT_PORT"] = str(health_server)
    env["LOOM_PREFLIGHT_MIGRATE"] = "true"
    r = subprocess.run(
        ["bash", str(repo / "dev" / "redeploy_preflight.sh"), "--target", "target"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "target adds 1 migration file(s) this checkout lacks" in r.stdout
    assert "20_b.sql" in r.stdout and "10_a.sql" not in r.stdout.split("target adds", 1)[1]
    # a target that is not a local rev (a bare build sha from /v1/health) skips the step quietly
    r = _run(health_server, "--target", "deadbee")
    assert "target adds" not in r.stdout
