"""Each sign-in path takes its issue time before it reads the grant cutoffs.

A cutoff can land while a mint runs, after its read; it is written again once
its insert returned (`revocation.end_grant_claims`), so the token or code must
carry a time from before the read for that second cutoff to postdate it."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth import login, oauth, oidc
from server.auth.jwt import verify_token
from tests.unit.oauth_store_fake import MemoryOAuthStore

_IDENTITY = ("alice", "acme", ["alice"], ["writer"])


async def _slow_identity(*_a: Any, **_k: Any) -> tuple[str, str, list[str], list[str]]:
    await asyncio.sleep(2.1)  # the cutoff read: an issue time after it is >= before + 2
    return _IDENTITY


def test_a_password_sign_in_is_issued_before_its_cutoff_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(login, "resolve_user_credentials", _slow_identity)
    app = FastAPI()
    app.include_router(login.router)
    before = int(time.time())
    r = TestClient(app).post("/v1/auth/login", json={"identifier": "alice", "password": "pw"})
    assert r.status_code == 200
    assert verify_token(r.json()["token"]).issued_at - before <= 1


def test_an_authorize_issues_its_code_before_its_cutoff_read(
    monkeypatch: pytest.MonkeyPatch,
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
    assert int(code["exp"]) - oauth._CODE_TTL - before <= 1


async def test_an_oidc_callback_issues_its_code_before_its_cutoff_read(
    monkeypatch: pytest.MonkeyPatch,
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
    assert int(code["exp"]) - oauth._CODE_TTL - before <= 1
