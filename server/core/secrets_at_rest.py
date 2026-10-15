"""Credentials stored in a loom table (a repository token, a tracker source's
password), sealed with a Fernet key derived from the deployment's JWT secret
and a per-purpose salt, so one purpose's ciphertext never opens another's."""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Any

from server.settings import settings

log = logging.getLogger(__name__)


def _fernet(purpose: str, secret: str) -> Any:
    from cryptography.fernet import Fernet  # pyjwt[crypto] guarantees it

    if not secret:
        raise RuntimeError(f"this deployment has no JWT secret; a {purpose} cannot be stored")
    salt = f"loom.{purpose}:".encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(salt + secret.encode()).digest()))


def _openers(purpose: str) -> Any:
    """The root secret's key, then LOOM_JWT_SECRET_PREVIOUS's during a rotation."""
    from cryptography.fernet import MultiFernet

    keys = [_fernet(purpose, str(settings.jwt_secret))]
    if settings.jwt_secret_previous:
        keys.append(_fernet(purpose, str(settings.jwt_secret_previous)))
    return MultiFernet(keys)


def seal(purpose: str, plain: str) -> str:
    """The ciphertext of `plain` for `purpose`, under the root secret; '' for ''."""
    if not plain:
        return ""
    return str(_fernet(purpose, str(settings.jwt_secret)).encrypt(plain.encode()).decode())


def unseal(purpose: str, enc: str, *, warn: bool = True) -> str:
    """The plaintext, or '' for '' and for a value that no longer decrypts (a
    rotated secret without LOOM_JWT_SECRET_PREVIOUS makes the credential
    unusable, not fatal). `warn=False` leaves the warning to a caller that
    reports it once."""
    if not enc:
        return ""
    try:
        return str(_openers(purpose).decrypt(enc.encode()).decode())
    except Exception:  # noqa: BLE001 — a rotated secret: the credential is unusable, say so
        if warn:
            log.warning("a stored %s no longer decrypts (secret rotated?)", purpose)
        return ""


def resealed(purpose: str, enc: str) -> str | None:
    """`enc` sealed again under the root secret when only
    LOOM_JWT_SECRET_PREVIOUS opens it; None when it already opens with the
    root secret, or with neither (nothing to rewrite)."""
    from cryptography.fernet import InvalidToken

    if not enc:
        return None
    try:
        _fernet(purpose, str(settings.jwt_secret)).decrypt(enc.encode())
        return None
    except InvalidToken:
        pass
    try:
        return str(_openers(purpose).rotate(enc.encode()).decode())
    except InvalidToken:
        return None
