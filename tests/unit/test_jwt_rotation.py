"""JWT kid-based key rotation.

In-process unit-style tests against `server.auth.jwt` + `server.settings`.
No HTTP round-trip needed; the change is in the verify path and is
exercised by mocking the settings' secret map.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest

from server.auth.jwt import TokenError, mint_token, verify_token
from server.settings import settings


@pytest.fixture
def kid_rotation(monkeypatch):
    """Install a two-key HS256 map: old + new. Active = new.

    Forces HS256 — rotation semantics are tested with shared secrets;
    the RS256 path lives in test_jwt_rs256.py.
    """
    secret_map = {
        "2026-Q1-old": "old-key-32-bytes-long-padding-xxx",
        "2026-Q2-new": "new-key-32-bytes-long-padding-yyy",
    }
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(
        settings,
        "jwt_secrets_json",
        '{"2026-Q1-old": "old-key-32-bytes-long-padding-xxx", '
        '"2026-Q2-new": "new-key-32-bytes-long-padding-yyy"}',
    )
    monkeypatch.setattr(settings, "jwt_active_kid", "2026-Q2-new")
    yield secret_map


def test_mint_writes_active_kid_into_header(kid_rotation) -> None:
    token = mint_token(
        subject="sa-x",
        org="o",
        namespaces=["n"],
        roles=["VIEWER"],
    )
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == "2026-Q2-new"


def test_token_from_old_kid_still_verifies_during_rotation(kid_rotation) -> None:
    """An old-kid token (minted before rotation) must still verify until
    its TTL expires."""
    old_token = mint_token(
        subject="sa-x",
        org="o",
        namespaces=["n"],
        roles=["VIEWER"],
        kid="2026-Q1-old",
    )
    info = verify_token(old_token)
    assert info.subject == "sa-x"


def test_token_from_new_kid_verifies(kid_rotation) -> None:
    new_token = mint_token(
        subject="sa-x",
        org="o",
        namespaces=["n"],
        roles=["VIEWER"],
        kid="2026-Q2-new",
    )
    info = verify_token(new_token)
    assert info.subject == "sa-x"


def test_unknown_kid_rejected(kid_rotation) -> None:
    """A token from a kid not in the map is rejected — used for the
    post-rotation phase after dropping old kids."""
    foreign = pyjwt.encode(
        {
            "sub": "rogue",
            "org": "o",
            "namespaces": ["n"],
            "roles": [],
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": 1_000_000_000,
            "exp": 9_000_000_000,
        },
        "some-foreign-secret",
        algorithm="HS256",
        headers={"kid": "rogue-kid-2099"},
    )
    with pytest.raises(TokenError, match="kid"):
        verify_token(foreign)


def test_kid_in_map_but_wrong_secret_still_fails(kid_rotation) -> None:
    """A token signed with a different secret than the one mapped to its
    claimed kid must fail signature verification."""
    forged = pyjwt.encode(
        {
            "sub": "rogue",
            "org": "o",
            "namespaces": ["n"],
            "roles": [],
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": 1_000_000_000,
            "exp": 9_000_000_000,
        },
        "wrong-secret",
        algorithm="HS256",
        headers={"kid": "2026-Q2-new"},
    )
    with pytest.raises(TokenError):
        verify_token(forged)


def test_legacy_single_secret_mode_still_works(monkeypatch) -> None:
    """When jwt_secrets_json is empty, jwt_secret + jwt_active_kid are used
    as a one-entry map — back-compat for the pre-rotation single-secret path."""
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(settings, "jwt_secrets_json", "")
    monkeypatch.setattr(settings, "jwt_secret", "legacy-secret-32-bytes-long-padding")
    monkeypatch.setattr(settings, "jwt_active_kid", "default")

    token = mint_token(
        subject="legacy-sa",
        org="o",
        namespaces=["n"],
        roles=["VIEWER"],
    )
    info = verify_token(token)
    assert info.subject == "legacy-sa"
    # Header should still carry the kid for verification
    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == "default"


def test_legacy_token_without_kid_header_falls_back(monkeypatch) -> None:
    """A token without a `kid` header verifies if its signature matches
    the active kid's secret."""
    monkeypatch.setattr(settings, "jwt_algorithm", "HS256")
    monkeypatch.setattr(settings, "jwt_secrets_json", "")
    monkeypatch.setattr(settings, "jwt_secret", "legacy-secret-32-bytes-long-padding")
    monkeypatch.setattr(settings, "jwt_active_kid", "default")

    # Encode in the back-compat way: no `kid` header
    import time as t

    payload = {
        "sub": "old",
        "org": "o",
        "namespaces": ["n"],
        "roles": ["VIEWER"],
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(t.time()),
        "exp": int(t.time()) + 3600,
        "sa": True,
    }
    legacy_token = pyjwt.encode(
        payload,
        "legacy-secret-32-bytes-long-padding",
        algorithm="HS256",
        # no headers={"kid": ...}
    )
    info = verify_token(legacy_token)
    assert info.subject == "old"
