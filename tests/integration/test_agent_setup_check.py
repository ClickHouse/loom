"""The agent setup page's check script, rendered as a release renders it and run as an
agent runs it, against the server under test: every check passes, a release candidate's
pin matches its final, and a wrong pin is a FAIL line, exit 1 and no write."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import httpx
import pytest

from scripts.release import agent_setup
from tests.integration._resources import unique_org


def _run(
    base_url: str, version: str, commit: str, home: Path, org: str
) -> subprocess.CompletedProcess[str]:
    text = agent_setup.render(agent_setup.PAGE.read_text(), version, commit)
    script = home / "loom-setup-check.py"
    script.write_text(agent_setup.check_script(text))
    # An empty home: no ~/.loom/.env of this machine's own stack reaches the bind check.
    env = {
        **os.environ,
        "HOME": str(home),
        "SETUP_CHECK_URL": base_url,
        "SETUP_CHECK_ORG": org,
    }
    return subprocess.run(
        [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=300
    )


@pytest.fixture
def control_plane_url(control_plane_url: str) -> str:
    """The script refuses a non-loopback server and sends no token, as on the stack it is
    written for: skip a remote or signed-in suite server."""
    if urllib.parse.urlsplit(control_plane_url).hostname not in ("localhost", "127.0.0.1", "::1"):
        pytest.skip("the setup check runs only against a loopback server")
    probe = {"org": "dev", "namespace": "agent-setup-check", "agent": "*"}
    if httpx.post(f"{control_plane_url}/v1/memory.list", json=probe, timeout=10).status_code == 401:
        pytest.skip("the setup check sends no token")
    return control_plane_url


def _served(base_url: str) -> tuple[str, str]:
    health = httpx.get(f"{base_url}/v1/health", timeout=10).json()
    return health["version"], health["build_commit"]


def test_the_page_checks_pass_on_a_served_install(control_plane_url: str, tmp_path: Path) -> None:
    done = _run(control_plane_url, *_served(control_plane_url), tmp_path, unique_org("agentsetup"))
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    for name in ("server", "memory", "knowledge", "cleanup"):
        assert any(re.match(rf"  \[PASS\]  {name}\s", line) for line in lines), done.stdout
    assert lines[-1] == "All 4 checks passed."


def test_a_release_candidate_pin_matches_its_final(control_plane_url: str, tmp_path: Path) -> None:
    version, commit = _served(control_plane_url)
    done = _run(control_plane_url, f"{version}-rc.1", commit, tmp_path, unique_org("agentsetup"))
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_wrong_pin_fails_and_writes_nothing(control_plane_url: str, tmp_path: Path) -> None:
    org = unique_org("agentsetup")
    done = _run(control_plane_url, "0.0.0", "0" * 40, tmp_path, org)
    assert done.returncode == 1, done.stdout + done.stderr
    check_lines = [line for line in done.stdout.splitlines() if line.startswith("  [")]
    assert len(check_lines) == 1, done.stdout
    assert re.match(r"  \[FAIL\]  server\s+serves .*; the pins name 0.0.0", check_lines[0])
    assert done.stdout.splitlines()[-1] == "1 check(s) failed: server"
    where = {"org": org, "namespace": "agent-setup-check", "agent": "*"}
    memories = httpx.post(f"{control_plane_url}/v1/memory.list", json=where, timeout=10).json()
    documents = httpx.post(f"{control_plane_url}/v1/knowledge.list", json=where, timeout=10).json()
    assert memories["entries"] == [] and documents["rows"] == []
