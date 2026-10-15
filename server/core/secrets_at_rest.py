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


def _fernet(purpose: str) -> Any:
    from cryptography.fernet import Fernet  # pyjwt[crypto] guarantees it

    secret = str(settings.jwt_secret)
    if not secret:
        raise RuntimeError(f"this deployment has no JWT secret; a {purpose} cannot be stored")
    salt = f"loom.{purpose}:".encode()
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(salt + secret.encode()).digest()))


def seal(purpose: str, plain: str) -> str:
    """The ciphertext of `plain` for `purpose`; '' for ''."""
    if not plain:
        return ""
    return str(_fernet(purpose).encrypt(plain.encode()).decode())


def unseal(purpose: str, enc: str) -> str:
    """The plaintext, or '' for '' and for a value that no longer decrypts (a
    rotated secret makes the credential unusable, not fatal)."""
    if not enc:
        return ""
    try:
        return str(_fernet(purpose).decrypt(enc.encode()).decode())
    except Exception:  # noqa: BLE001 — a rotated secret: the credential is unusable, say so
        log.warning("a stored %s no longer decrypts (secret rotated?)", purpose)
        return ""


def open_sealed(purpose: str, enc: str) -> str | None:
    """`unseal` for a value from outside (a cookie): None, and no log line,
    when it was not sealed here for `purpose`."""
    if not enc:
        return None
    try:
        return str(_fernet(purpose).decrypt(enc.encode()).decode())
    except Exception:  # noqa: BLE001 — forged or foreign: the caller refuses it
        return None
