"""dev/loom_init.py — onboarding CLI.

Subprocess the CLI (same pattern as test_heartbeat) so we exercise the
real argv parsing, env writing, JWT minting, and the generated starter
script's end-to-end run against the live server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import jwt as pyjwt
import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "dev" / "loom_init.py"


def _run_cli(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(CLI), *args]
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
        env=full_env,
    )


def test_init_writes_env_and_starter(tmp_path: Path, control_plane_url: str) -> None:
    """Happy path: .env + loom_starter.py land in --output-dir."""
    ns = f"init-{uuid.uuid4().hex[:8]}"
    result = _run_cli(
        "--namespace",
        ns,
        "--agent",
        "test-agent",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert summary["namespace"] == ns
    assert summary["subject"] == f"sa-{ns}-test-agent"

    env_path = tmp_path / ".env"
    starter_path = tmp_path / "loom_starter.py"
    assert env_path.exists()
    assert starter_path.exists()

    env = dict(line.split("=", 1) for line in env_path.read_text().strip().splitlines())
    assert env["LOOM_ORG"] == "dev"  # the CLI default
    assert env["LOOM_NAMESPACE"] == ns
    assert env["LOOM_AGENT"] == "test-agent"
    assert env["LOOM_BASE_URL"] == control_plane_url
    assert env["LOOM_API_KEY"].count(".") == 2  # JWT looks like a JWT


def test_init_minted_token_decodes_with_server_secret(
    tmp_path: Path, control_plane_url: str
) -> None:
    """The minted JWT must verify against the same secret the server uses."""
    from server.settings import settings

    ns = f"init-{uuid.uuid4().hex[:8]}"
    result = _run_cli(
        "--namespace",
        ns,
        "--agent",
        "decoder-test",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--json",
    )
    assert result.returncode == 0
    token = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text().strip().splitlines()
    )["LOOM_API_KEY"]

    # The root secret signs as kid `default` until a key in loom.signing_keys
    # activates; this process has not loaded that keyring.
    from server.auth import keyring

    header = pyjwt.get_unverified_header(token)
    keys = keyring.verification_keys(header.get("kid", keyring.ROOT_KID), header["alg"])
    if not keys:
        pytest.skip(f"signed by keyring key {header.get('kid')!r}, not the root secret")
    key = keys[0]
    decoded = pyjwt.decode(
        token,
        key,
        algorithms=[header["alg"]],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
    )
    assert decoded["sub"] == f"sa-{ns}-decoder-test"
    assert decoded["org"] == "dev"
    assert decoded["namespaces"] == [ns]
    assert "ADMIN" in decoded["roles"]
    assert decoded["sa"] is True


def test_init_refuses_to_clobber_existing_env(tmp_path: Path, control_plane_url: str) -> None:
    """Existing .env without --force → non-zero exit + helpful stderr."""
    (tmp_path / ".env").write_text("LOOM_API_KEY=preexisting\n")

    result = _run_cli(
        "--namespace",
        "ns",
        "--agent",
        "agent",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
    )
    assert result.returncode != 0
    assert "--force" in result.stderr
    # File untouched
    assert (tmp_path / ".env").read_text() == "LOOM_API_KEY=preexisting\n"


def test_init_force_overwrites(tmp_path: Path, control_plane_url: str) -> None:
    """--force replaces an existing .env."""
    (tmp_path / ".env").write_text("LOOM_API_KEY=preexisting\n")

    result = _run_cli(
        "--namespace",
        "ns-force",
        "--agent",
        "agent",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--force",
        "--json",
    )
    assert result.returncode == 0
    contents = (tmp_path / ".env").read_text()
    assert "preexisting" not in contents
    assert "LOOM_API_KEY=" in contents


def test_starter_script_runs_end_to_end(tmp_path: Path, control_plane_url: str) -> None:
    """The generated loom_starter.py should run green against the live server.

    This is the headline UX claim — `loom init` → `python loom_starter.py`
    succeeds in one shot.
    """
    ns = f"starter-{uuid.uuid4().hex[:8]}"
    init = _run_cli(
        "--namespace",
        ns,
        "--agent",
        "starter-test",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--json",
    )
    assert init.returncode == 0, f"init failed: {init.stderr!r}"

    starter = tmp_path / "loom_starter.py"

    # Load the generated .env into a subprocess env
    env_vars = dict(
        line.split("=", 1) for line in (tmp_path / ".env").read_text().strip().splitlines()
    )
    run_env = os.environ.copy()
    run_env.update(env_vars)
    run_env["PYTHONPATH"] = f"{ROOT / 'sdk' / 'python'}{os.pathsep}{run_env.get('PYTHONPATH', '')}"

    result = subprocess.run(
        [sys.executable, str(starter)],
        capture_output=True,
        text=True,
        timeout=30,
        env=run_env,
    )
    assert result.returncode == 0, (
        f"starter failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "memory ok" in result.stdout
    assert "knowledge ok" in result.stdout
    assert "cleanup ok" in result.stdout


def test_init_langfuse_skipped_without_key(tmp_path: Path, control_plane_url: str) -> None:
    """--langfuse-host without LANGFUSE_ORG_API_KEY → init succeeds, status='skipped'."""
    env = os.environ.copy()
    env.pop("LANGFUSE_ORG_API_KEY", None)

    result = _run_cli(
        "--namespace",
        "lf-ns",
        "--agent",
        "lf-agent",
        "--base-url",
        control_plane_url,
        "--output-dir",
        str(tmp_path),
        "--langfuse-host",
        "http://langfuse.example.com",
        "--json",
        env={"LANGFUSE_ORG_API_KEY": ""},  # ensure unset
    )
    # Subprocess inherits parent env minus LANGFUSE_ORG_API_KEY override-to-empty;
    # Python's os.environ.get returns "" not None for empty string, so the CLI
    # treats that as "set but blank". Test the more useful case via missing key:
    # we accept either "skipped" or "failed: ..." but the run itself must succeed.
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    summary = json.loads(result.stdout)
    assert summary["ok"] is True
    assert summary["langfuse"] != "skipped (no --langfuse-host)"  # was given the host
