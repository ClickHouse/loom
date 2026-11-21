"""A retained registration remembers the key that actually verified its claim."""

from __future__ import annotations

import time

import jwt
import pytest

from server.auth import jwt as auth_jwt
from server.auth import keyring, revocation
from server.core import cell
from server.settings import settings

_FIRST = "first-signing-material-with-more-than-thirty-two-bytes"
_SECOND = "second-signing-material-with-more-than-thirty-two-bytes"


def _token(secret: str) -> str:
    return jwt.encode(
        {
            "sub": "registrant",
            "org": "org",
            "namespaces": ["team"],
            "roles": ["org_admin"],
            "sa": False,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": int(time.time()),
            "jti": "registration-token",
        },
        secret,
        algorithm="HS256",
        headers={"kid": "signer"},
    )


@pytest.fixture
def key_view(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    # External snapshots are healthy; the signature/claim verifier remains real.
    keys = [_FIRST, _SECOND]
    monkeypatch.setattr(cell, "gate_reason", lambda: "")
    monkeypatch.setattr(keyring, "loaded", lambda: True)
    monkeypatch.setattr(keyring, "stale", lambda: False)
    monkeypatch.setattr(keyring, "verification_keys", lambda kid, algorithm: list(keys))
    monkeypatch.setattr(revocation, "loaded", lambda: True)
    monkeypatch.setattr(revocation, "snapshot_age_seconds", lambda: 0)
    monkeypatch.setattr(revocation, "is_revoked", lambda *args, **kwargs: False)
    return keys


def test_retained_authority_pins_successful_key_not_first_same_kid_candidate(
    key_view: list[str],
) -> None:
    auth = auth_jwt.verify_token(_token(_SECOND))
    authority = auth.signing_authority
    assert authority is not None and authority.current()
    assert (authority.issuer, authority.kid, authority.algorithm) == (
        settings.jwt_issuer,
        "signer",
        "HS256",
    )
    key_view.remove(_SECOND)
    assert not authority.current()  # A different still-valid key under this kid cannot inherit it.
    assert _FIRST not in repr(auth) and _SECOND not in repr(auth)


@pytest.mark.parametrize("failure", ["unloaded", "stale", "untrusted-issuer"])
def test_retained_authority_requires_current_signing_and_issuer_configuration(
    monkeypatch: pytest.MonkeyPatch,
    key_view: list[str],
    failure: str,
) -> None:
    authority = auth_jwt.verify_token(_token(_SECOND)).signing_authority
    assert authority is not None
    if failure == "unloaded":
        monkeypatch.setattr(keyring, "loaded", lambda: False)
    elif failure == "stale":
        monkeypatch.setattr(keyring, "stale", lambda: True)
    else:
        monkeypatch.setattr(settings, "jwt_issuer", "changed-issuer")
    assert not authority.current()


def test_explicit_offline_secret_verification_creates_no_persistable_authority(
    key_view: list[str],
) -> None:
    auth = auth_jwt.verify_token(_token(_SECOND), secret=_SECOND)
    assert auth.subject == "registrant" and auth.signing_authority is None
