"""Argon2id password hashing.

argon2id is the OWASP-recommended modern password hash — memory-hard,
constant-time verify, parameterized for tunable cost. argon2-cffi
embeds the C reference impl and handles parameter encoding into the
output string so we don't manage salt/iterations/memory separately.

A hash looks like:
    $argon2id$v=19$m=65536,t=3,p=4$<salt>$<digest>

Verify is constant-time and rejects on any mismatch or malformed input.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher

# The length a chosen password must have, wherever a person sets one.
PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH = 8, 256

# Single shared hasher — its config is fine for our threat model
# (interactive login, not bulk-issued tokens). Tune memory/time
# upward if running on hardware where verifications are >100ms.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """Hash a plain-text password. Returns argon2id-encoded string."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time verify. Returns False for ANY error (mismatch,
    malformed hash, empty hash) — never raises."""
    if not stored_hash or not password:
        return False
    try:
        _hasher.verify(stored_hash, password)
        return True
    except Exception:  # noqa: BLE001 — any verify error is a mismatch (closed)
        return False


# One hash or verify is ~40 ms of CPU; argon2-cffi releases the GIL while it
# runs, so the request paths hand it to a thread instead of stalling every
# other request on the event loop for that long.


async def hash_password_off_loop(password: str) -> str:
    """`hash_password` on a worker thread."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_off_loop(stored_hash: str, password: str) -> bool:
    """`verify_password` on a worker thread."""
    return await asyncio.to_thread(verify_password, stored_hash, password)
