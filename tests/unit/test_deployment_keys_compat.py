"""A read-replica of the build before 033 checks the published `_keys` with
its own `check_keys` at boot. The new build must publish what that check
accepts, or an old replica restarting mid-upgrade cannot boot."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import pytest

from server.core import deployment_config as dc
from server.settings import Settings


def _fp(secret: str) -> str:
    return hmac.new(
        secret.encode(), b"loom.deployment-config.key-check", hashlib.sha256
    ).hexdigest()[:32]


def _old_replica_check(secret_map: dict[str, str], active_kid: str, root: str, theirs: Any) -> None:
    """main's `check_keys` (before 033), verbatim but for reading the old
    settings from arguments."""
    if not isinstance(theirs, dict):
        return
    ours = {
        "signing": {kid: _fp(key) for kid, key in sorted(secret_map.items())},
        "active_kid": active_kid,
        "sealing": _fp(root),
    }
    their_signing = theirs.get("signing") or {}
    problems = []

    def mismatch(kid: str, signer: str, holder: str, held: dict[str, Any]) -> None:
        if kid not in held:
            problems.append(f"{signer} signs with kid {kid!r}, which {holder} lacks")
        else:
            problems.append(f"{holder} holds a different key for kid {kid!r}")

    their_kid = str(theirs.get("active_kid") or "")
    if ours["signing"].get(their_kid) != their_signing.get(their_kid):
        mismatch(their_kid, "the main Loom", "this process", ours["signing"])
    our_kid = ours["active_kid"]
    if our_kid != their_kid and their_signing.get(our_kid) != ours["signing"][our_kid]:
        mismatch(our_kid, "this process", "the main Loom", their_signing)
    if theirs.get("sealing") and theirs.get("sealing") != ours["sealing"]:
        problems.append("LOOM_JWT_SECRET differs, so stored credentials do not open")
    if problems:
        raise ValueError("; ".join(problems))


def test_an_old_replica_with_the_same_root_accepts_the_new_snapshot() -> None:
    root = "s" * 40
    published = dc.snapshot(Settings(_env_file=None, jwt_secret=root))  # type: ignore[call-arg]
    _old_replica_check({"default": root}, "default", root, published[dc.KEYS_ENTRY])
    with pytest.raises(ValueError, match="different key"):
        _old_replica_check({"default": "t" * 40}, "default", "t" * 40, published[dc.KEYS_ENTRY])
