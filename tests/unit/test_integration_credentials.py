"""Integration credentials remain usable when a suite outlasts one token."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("provided", [False, True])
def test_sdk_fallback_credentials_between_tests(tmp_path: Path, provided: bool) -> None:
    (tmp_path / "conftest.py").write_text(
        """
import time

from server.auth import jwt, keyring
from tests.conftest import _load_repo_dotenv, _seed_test_api_key

clock = {"now": int(time.time())}
mint = jwt.mint_token

def mint_at_clock(**kwargs):
    return mint(**kwargs, issued_at=clock["now"])

def pytest_sessionstart(session):
    keyring.sign_with_root_only()
    jwt.mint_token = mint_at_clock
""",
        encoding="utf-8",
    )
    (tmp_path / "test_credentials.py").write_text(
        """
import os

import jwt
from conftest import clock

def check_token():
    token = os.environ["LOOM_API_KEY"]
    if os.environ["PROVIDED_TOKEN"]:
        assert token == os.environ["PROVIDED_TOKEN"]
    else:
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["exp"] > clock["now"]
        assert claims["org"] == "*"
        assert claims["roles"] == ["writer"]

def test_first():
    check_token()
    clock["now"] += 3601

def test_later():
    check_token()
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "LOOM_ENV_FILE": "",
        "LOOM_JWT_SECRET": "integration-credential-test-secret",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PROVIDED_TOKEN": "caller-token" if provided else "",
    }
    if provided:
        env["LOOM_API_KEY"] = env["PROVIDED_TOKEN"]
    else:
        env.pop("LOOM_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
