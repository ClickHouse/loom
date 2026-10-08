"""Stored credentials are sealed under a per-purpose key derived from the JWT
secret; the derivation matches what the code and tracker planes stored before
they shared it, so existing ciphertexts still open."""

from __future__ import annotations

import base64
import hashlib

import pytest
from cryptography.fernet import Fernet

from server.core.secrets_at_rest import seal, unseal
from server.settings import settings


def _legacy(salt: bytes, secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(salt + secret.encode()).digest()))


@pytest.mark.parametrize(
    ("purpose", "salt"),
    [
        ("code_repos.token", b"loom.code_repos.token:"),
        ("qa_sources.password", b"loom.qa_sources.password:"),
    ],
)
def test_stored_ciphertexts_still_open(
    monkeypatch: pytest.MonkeyPatch, purpose: str, salt: bytes
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    stored = _legacy(salt, "unit-test-secret").encrypt(b"s3cret").decode()
    assert unseal(purpose, stored) == "s3cret"
    assert unseal(purpose, seal(purpose, "s3cret")) == "s3cret"


def test_purposes_do_not_open_each_other_and_empty_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    enc = seal("code_repos.token", "ghp_x")
    assert "ghp_x" not in enc
    assert unseal("qa_sources.password", enc) == ""
    assert seal("code_repos.token", "") == "" and unseal("code_repos.token", "") == ""


def test_no_secret_refuses_to_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "")
    with pytest.raises(RuntimeError):
        seal("code_repos.token", "x")
