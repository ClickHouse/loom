"""A test end-user issuer: the service in front of Loom that signs a short-lived
token per end user (server/auth/gateway.py).

`python -m tests.end_user_issuer <dir>` writes a fresh RSA key to
`<dir>/end_user_key.pem` and prints the two environment lines a test run needs:
`LOOM_JWT_END_USER_ISSUERS_JSON` for the server (the public key under
`ISSUER`) and `LOOM_TEST_END_USER_KEY` for the tests (the private key's path).
`mint(...)` signs a token with that key.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

ISSUER = "test-end-user-issuer"
KID = "test-end-user-1"
# The orgs its tokens may name: the unit tests' and the integration suite's
# (`unique_org`: it-<label>-<hex>).
ORGS = ("acme", "globex", "it-*")
_KEY_ENV = "LOOM_TEST_END_USER_KEY"


def new_key() -> tuple[str, str]:
    """(private PEM, public PEM) of a fresh RSA key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def issuers_setting(public_pem: str, **spec: object) -> str:
    """The `LOOM_JWT_END_USER_ISSUERS_JSON` value that trusts `public_pem` for
    `ORGS` (or what `spec` says instead)."""
    return json.dumps({ISSUER: {"orgs": list(ORGS), "keys": {KID: public_pem}, **spec}})


def configured() -> bool:
    """Whether this run's server trusts the test issuer (its key is at hand)."""
    return bool(os.environ.get(_KEY_ENV))


def mint(
    org: str,
    user: str,
    *,
    agent: str = "",
    ttl_seconds: int = 300,
    private_pem: str | None = None,
    audience: str | None = None,
    **claims: object,
) -> str:
    """An end-user token for `user:<user>` in `org`, signed with the key of this
    run (or `private_pem`); `agent` names `agent:<agent>`."""
    import jwt

    from server.settings import settings

    if private_pem is None:
        private_pem = Path(os.environ[_KEY_ENV]).read_text()
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": ISSUER,
        "aud": audience or settings.jwt_audience,
        "sub": f"user:{user}",
        "org": org,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
        **claims,
    }
    if agent:
        payload["agent"] = f"agent:{agent}"
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": KID})


def main(directory: str) -> None:
    private, public = new_key()
    path = Path(directory) / "end_user_key.pem"
    path.write_text(private)
    path.chmod(0o600)
    print(f"LOOM_JWT_END_USER_ISSUERS_JSON={issuers_setting(public)}")
    print(f"{_KEY_ENV}={path}")


if __name__ == "__main__":
    main(sys.argv[1])
