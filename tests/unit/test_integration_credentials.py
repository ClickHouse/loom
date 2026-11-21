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
import dataclasses
import os
import time

import jwt
from conftest import clock
from server.auth import keyring
from server.settings import settings

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
    # In-process handlers can load the server keyring without a refresh worker.
    keyring._view = dataclasses.replace(
        keyring._view,
        read_at=time.monotonic() - settings.jwt_revocation_max_stale_seconds - 1,
    )
    assert keyring.stale()

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
    # The child runs its two tests in order with no plugins: a parent's addopts
    # (coverage, randomly) would fail it or reorder it.
    env.pop("PYTEST_ADDOPTS", None)
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


@pytest.mark.parametrize("provided", [False, True])
@pytest.mark.parametrize("body_fails", [False, True])
def test_client_signer_restores_exact_snapshot_on_every_exit(monkeypatch, provided, body_fails):
    import dataclasses
    import time

    import jwt

    from server.auth import keyring
    from server.core.errors import BackendUnavailableError
    from server.settings import settings
    from tests.conftest import _seed_test_api_key

    stale = dataclasses.replace(
        keyring._view,
        loaded=True,
        read_at=time.monotonic() - settings.jwt_revocation_max_stale_seconds - 1,
    )
    monkeypatch.setattr(keyring, "_view", stale)
    if provided:
        monkeypatch.setenv("LOOM_API_KEY", "caller-token")
    else:
        monkeypatch.delenv("LOOM_API_KEY", raising=False)
    with pytest.raises(BackendUnavailableError, match="stale"):
        keyring.signing_key()

    lifecycle = _seed_test_api_key.__wrapped__(None)
    next(lifecycle)
    assert keyring._view is not stale
    token = os.environ["LOOM_API_KEY"]
    if provided:
        assert token == "caller-token"
    else:
        assert jwt.get_unverified_header(token)["kid"] == "default"
        assert jwt.decode(token, options={"verify_signature": False})["org"] == "*"
    # A handler can replace its own snapshot; teardown still restores the original.
    keyring._view = dataclasses.replace(keyring._view, read_at=time.monotonic())
    if body_fails:
        with pytest.raises(RuntimeError, match="body failed"):
            lifecycle.throw(RuntimeError("body failed"))
    else:
        with pytest.raises(StopIteration):
            next(lifecycle)
    assert keyring._view is stale
    assert os.environ.get("LOOM_API_KEY") == ("caller-token" if provided else None)
    with pytest.raises(BackendUnavailableError, match="stale"):
        keyring.signing_key()
