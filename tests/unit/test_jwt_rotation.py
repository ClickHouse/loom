"""JWT kid-based key rotation through the signing keyring.

In-process tests against `server.auth.jwt` + `server.auth.keyring`, loaded
from a fake `loom.signing_keys`. RS256 tokens are covered in test_signing_keyring.py
and test_signing_keyring_guards.py, and against a server in
tests/integration/test_jwt_rs256.py.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

import jwt as pyjwt
import pytest

from server.auth import keyring
from server.auth.jwt import TokenError, mint_token, verify_token
from server.core.secrets_at_rest import seal
from server.settings import settings

_OLD = "old-key-32-bytes-long-padding-xxx"
_NEW = "new-key-32-bytes-long-padding-yyy"


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self.rows


def _row(kid: str, secret: str, *, minutes_ago: int, retired: bool = False) -> dict[str, Any]:
    return {
        "kid": kid,
        "algorithm": "HS256",
        "sealed_key": seal("signing_key", secret),
        "public_pem": "",
        "activate_at": keyring._now() - timedelta(minutes=minutes_ago),
        "retired": retired,
    }


def _load(*rows: dict[str, Any]) -> None:
    asyncio.run(keyring.refresh(ch=_CH(list(rows))))


def _claims(sub: str = "rogue") -> dict[str, Any]:
    return {
        "sub": sub,
        "org": "o",
        "namespaces": ["n"],
        "roles": [],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "jwt_secret", "root-secret-32-bytes-long-padding")
    keyring._reset_for_tests()
    yield
    keyring._reset_for_tests()


@pytest.fixture
def kid_rotation() -> None:
    """Two HS256 keys: the old one, and the newer one that now signs."""
    _load(_row("2026-Q1-old", _OLD, minutes_ago=60), _row("2026-Q2-new", _NEW, minutes_ago=5))


def test_mint_writes_active_kid_into_header(kid_rotation) -> None:
    token = mint_token(subject="sa-x", org="o", namespaces=["n"], roles=["VIEWER"])
    assert pyjwt.get_unverified_header(token)["kid"] == "2026-Q2-new"
    assert verify_token(token).subject == "sa-x"


def test_token_from_old_kid_still_verifies_during_rotation(kid_rotation) -> None:
    """An old-kid token (minted before rotation) still verifies until its kid
    is retired."""
    old_token = pyjwt.encode(
        _claims("sa-x"), _OLD, algorithm="HS256", headers={"kid": "2026-Q1-old"}
    )
    assert verify_token(old_token).subject == "sa-x"


def test_unknown_kid_rejected(kid_rotation) -> None:
    foreign = pyjwt.encode(
        _claims(), "some-foreign-secret", algorithm="HS256", headers={"kid": "rogue-kid-2099"}
    )
    with pytest.raises(TokenError, match="kid"):
        verify_token(foreign)


def test_kid_in_map_but_wrong_secret_still_fails(kid_rotation) -> None:
    """A token signed with another secret than its claimed kid's fails the
    signature check."""
    forged = pyjwt.encode(
        _claims(),
        "wrong-secret-32-bytes-long-padding",
        algorithm="HS256",
        headers={"kid": "2026-Q2-new"},
    )
    with pytest.raises(TokenError):
        verify_token(forged)


def test_legacy_single_secret_mode_still_works() -> None:
    """An empty keyring signs with the root secret as kid `default`."""
    token = mint_token(subject="legacy-sa", org="o", namespaces=["n"], roles=["VIEWER"])
    assert verify_token(token).subject == "legacy-sa"
    assert pyjwt.get_unverified_header(token)["kid"] == "default"


def test_legacy_token_without_kid_header_falls_back(kid_rotation) -> None:
    """A token without a `kid` header is checked against the root secret."""
    legacy_token = pyjwt.encode(
        _claims("old"), "root-secret-32-bytes-long-padding", algorithm="HS256"
    )
    assert verify_token(legacy_token).subject == "old"
    with pytest.raises(TokenError):
        verify_token(pyjwt.encode(_claims("old"), _NEW, algorithm="HS256"))


def test_retiring_a_kid_kills_its_tokens_and_the_new_kid_works() -> None:
    """The remediation rotation: mint under A, activate B and retire A. The A
    token is refused; a B token verifies."""
    _load(_row("A", "a" * 32, minutes_ago=60))
    before = mint_token(subject="alice", org="o", namespaces=["n"], roles=["writer"])
    assert pyjwt.get_unverified_header(before)["kid"] == "A"
    assert verify_token(before).subject == "alice"

    _load(_row("A", "a" * 32, minutes_ago=60, retired=True), _row("B", "b" * 32, minutes_ago=1))
    with pytest.raises(TokenError, match="kid"):
        verify_token(before)
    after = mint_token(subject="alice", org="o", namespaces=["n"], roles=["writer"])
    assert pyjwt.get_unverified_header(after)["kid"] == "B"
    assert verify_token(after).subject == "alice"
