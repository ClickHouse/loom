"""Each sign-in path takes its issue time before it reads the grant cutoffs.

A cutoff can land while a mint runs, after its read; it is written again once
its insert returned (`revocation.write_grant_cutoffs`), so the token or code must
carry a time from before the read for that second cutoff to postdate it."""

from __future__ import annotations

import time
from typing import Any

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import login, oauth, oidc
from tests.unit.oauth_store_fake import MemoryOAuthStore

_IDENTITY = ("alice", "acme", ["alice"], ["writer"])


def _iat(token: str) -> int:
    """The token's iat, unverified: a verify would refuse an iat past the real
    clock and hide what the assertion checks."""
    return int(pyjwt.decode(token, options={"verify_signature": False})["iat"])


_READ_TAKES = 100  # seconds the cutoff read takes on the fake clock


@pytest.fixture(autouse=True)
def _clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """`time.time` that moves only when the cutoff read runs."""
    now = {"t": float(int(time.time()))}
    monkeypatch.setattr(time, "time", lambda: now["t"])
    return now


@pytest.fixture
def _slow_identity(_clock: dict[str, float]) -> Any:
    async def _read(*_a: Any, **_k: Any) -> login.Identity:
        _clock["t"] += _READ_TAKES  # an issue time taken after the read shows it
        return _IDENTITY

    return _read


def test_a_password_sign_in_is_issued_before_its_cutoff_read(
    monkeypatch: pytest.MonkeyPatch, _slow_identity: Any
) -> None:
    monkeypatch.setattr(login, "resolve_user_credentials", _slow_identity)
    app = FastAPI()
    app.include_router(login.router)
    before = int(time.time())
    r = TestClient(app).post("/v1/auth/login", json={"identifier": "alice", "password": "pw"})
    assert r.status_code == 200
    assert _iat(r.json()["token"]) == before


def test_an_authorize_issues_its_code_before_its_cutoff_read(
    monkeypatch: pytest.MonkeyPatch, _slow_identity: Any
) -> None:
    store = MemoryOAuthStore()

    async def _valid(*_a: Any) -> None:
        return None

    monkeypatch.setattr(oauth, "_store", store)
    monkeypatch.setattr(oauth, "_validate_authorize_params", _valid)
    monkeypatch.setattr(login, "resolve_user_credentials", _slow_identity)
    app = FastAPI()
    app.include_router(oauth.router)
    before = int(time.time())
    r = TestClient(app, follow_redirects=False).post(
        "/oauth/authorize",
        data={
            "client_id": "c",
            "redirect_uri": "http://127.0.0.1:9/cb",
            "code_challenge": "x",
            "identifier": "alice",
            "password": "pw",
        },
    )
    assert r.status_code == 302
    (code,) = store.codes.values()
    assert int(code["exp"]) - oauth._CODE_TTL == before


async def test_an_oidc_callback_issues_its_code_before_its_cutoff_read(
    monkeypatch: pytest.MonkeyPatch, _slow_identity: Any
) -> None:
    store = MemoryOAuthStore()
    await store.save_pending(
        "s",
        {"nonce": "n", "client_id": "c", "redirect_uri": "http://r/cb", "code_challenge": "x"},
    )

    async def _exchange(**_k: Any) -> dict[str, str]:
        return {"id_token": "t"}

    async def _claims(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"email": "alice@acme.io", "email_verified": True}

    class _Req:
        query_params = {"code": "c", "state": "s"}

    monkeypatch.setattr(oauth, "_store", store)
    monkeypatch.setattr(oidc, "exchange_code", _exchange)
    monkeypatch.setattr(oidc, "verify_id_token", _claims)
    monkeypatch.setattr(oidc, "verified_email", lambda claims: "alice@acme.io")
    monkeypatch.setattr(login, "resolve_identity_for_email", _slow_identity)
    before = int(time.time())
    r = await oauth.oidc_callback(_Req())  # type: ignore[arg-type]
    assert r.status_code == 302
    (code,) = store.codes.values()
    assert int(code["exp"]) - oauth._CODE_TTL == before


def test_login_answers_409_too_many_spellings_with_its_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import TooManySpellingsError, register_exception_handler

    async def _too_many(*_a: Any, **_k: Any) -> Any:
        raise TooManySpellingsError("too many accounts")

    monkeypatch.setattr(login, "resolve_user_credentials", _too_many)
    app = FastAPI()
    register_exception_handler(app)
    app.include_router(login.router)
    r = TestClient(app).post("/v1/auth/login", json={"identifier": "alice", "password": "pw"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "TOO_MANY_SPELLINGS"
    assert "retry-after" not in r.headers and "#too_many_spellings" in r.headers["link"]


def test_a_sign_in_that_cannot_read_the_cutoffs_answers_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unavailable(*_a: Any, **_k: Any) -> Any:
        raise login.SignInUnavailable

    monkeypatch.setattr(login, "resolve_user_credentials", _unavailable)
    app = FastAPI()
    app.include_router(login.router)
    r = TestClient(app).post("/v1/auth/login", json={"identifier": "alice", "password": "pw"})
    assert r.status_code == 503 and r.headers["retry-after"] == "5"
